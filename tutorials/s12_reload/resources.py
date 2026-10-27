# ===== s12 快照 =====
# 来自 s11：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s11：只读资源发现、Skill 渐进加载与模板参数展开，参考 Pi resource-loader。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

import yaml


# 来自 s11：保持；PyYAML safe_load 负责完整 YAML，不用手工拆行冒充解析器。
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """输入 Markdown，输出元数据和正文；无头信息原样返回，坏 YAML 报错。"""
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    match = re.search(r"^---\s*$", text[4:], re.MULTILINE)
    if match is None:
        raise ValueError("frontmatter 缺少结束分隔符")
    metadata = yaml.safe_load(text[4 : 4 + match.start()])
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter 必须是映射")  # noqa: TRY004 - Markdown 格式统一报告 ValueError
    return metadata, text[4 + match.end() :].lstrip("\n")


# 来自 s11：保持；按 Pi 单次替换，参数中的 $1 不会再被解释。
def parse_command_args(text: str) -> list[str]:
    """按 Pi 规则拆引号参数；不执行 shell、不处理反斜杠转义。"""
    result, current, quote = [], "", None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in {"'", '"'}:
            quote = char
        elif char.isspace():
            if current:
                result.append(current)
                current = ""
        else:
            current += char
    if current:
        result.append(current)
    return result


def substitute_args(content: str, arguments: list[str]) -> str:
    """替换位置、全部、默认值和切片占位符，保持 Pi 的非递归语义。"""
    pattern = r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"

    def replace(match):
        default_target, default, start, length, simple = match.groups()
        if start is not None:
            index = max(0, int(start) - 1)
            return " ".join(
                arguments[index:] if length is None else arguments[index : index + int(length)]
            )
        target = default_target or simple
        value = (
            " ".join(arguments)
            if target in {"ARGUMENTS", "@"}
            else (arguments[int(target) - 1] if 0 < int(target) <= len(arguments) else "")
        )
        return value or default if default_target is not None else value

    return re.sub(pattern, replace, content)


@dataclass(frozen=True)
class Skill:
    """保存元数据与实际来源路径，正文只在显式使用或模型 read 时加载。"""

    name: str
    description: str
    path: Path
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class PromptTemplate:
    """保存命令名称、说明和当前已加载正文，不执行其中任何命令。"""

    name: str
    description: str
    content: str
    path: Path


# 来自 s11：保持；资源只从明确业务根和配置根发现，不扫描 Harness 项目。
class ResourceLoader:
    """加载上下文、Skills 和模板；产生系统提示与显式展开文本。

    输入：workspace、可选全局目录与显式资源；输出：元数据、诊断及提示文本。
    流程：上下文按全局/祖先到当前加载 → 发现元数据 → 输入 Hook 后展开资源。
    边界：默认上下文根为业务 workspace；不安装资源、不执行 Markdown、不递归索引业务源码。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        global_dir: Path | None = None,
        context_root: Path | None = None,
        skill_paths=(),
        prompt_paths=(),
        defaults=True,
    ):
        self.workspace = workspace.resolve(strict=True)
        self.context_root = (context_root or workspace).resolve(strict=True)
        if not self.workspace.is_relative_to(self.context_root):
            raise ValueError("上下文根必须包含 workspace")
        self.global_dir = global_dir.resolve() if global_dir else None
        self.skill_paths, self.prompt_paths, self.defaults = (
            tuple(skill_paths),
            tuple(prompt_paths),
            defaults,
        )
        self.skills: tuple[Skill, ...] = ()
        self.templates: tuple[PromptTemplate, ...] = ()
        self.context_files: tuple[tuple[Path, str], ...] = ()
        self.diagnostics = []

    def _skill_candidates(self, root: Path, seen: set[Path], *, include_root_files=True):
        """只遍历资源根；技能目录存在 SKILL.md 时停止下探，防止链接循环。"""
        # s11 修复：仅资源根接受松散 Markdown，子目录只认 SKILL.md，防止引用误注册。
        root = root.resolve()
        if root in seen:
            return
        seen.add(root)
        if root.is_file():
            yield root
        elif root.is_dir():
            if (root / "SKILL.md").is_file():
                yield root / "SKILL.md"
                return
            for child in sorted(root.iterdir()):
                if child.name.startswith(".") or child.name in {"node_modules", "__pycache__"}:
                    continue
                if child.is_dir():
                    yield from self._skill_candidates(child, seen, include_root_files=False)
                elif include_root_files and child.suffix == ".md":
                    yield child

    def load(self):
        """读取候选资源，坏文件记诊断；在末尾一次替换当前可用资源集合。"""
        contexts, skills, templates, diagnostics = [], {}, {}, []
        roots = []
        if self.defaults:
            if self.global_dir:
                roots.append(self.global_dir)
            ancestors, current = [], self.workspace
            while True:
                ancestors.insert(0, current)
                if current == self.context_root:
                    break
                current = current.parent
            roots.extend(ancestors)
        seen_context = set()
        for root in roots:
            # s11 保留：Pi 每层只取第一个存在的候选文件；Python Windows 同名大小写去重。
            for name in ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"):
                path = root / name
                if path.is_file():
                    try:
                        canonical = path.resolve()
                        if canonical not in seen_context:
                            contexts.append((canonical, path.read_text(encoding="utf-8-sig")))
                            seen_context.add(canonical)
                        break
                    except OSError as error:
                        diagnostics.append({"path": str(path), "error": str(error)})
        skill_roots = []
        prompt_roots = []
        if self.defaults:
            if self.global_dir:
                skill_roots.append(self.global_dir / "skills")
                prompt_roots.append(self.global_dir / "prompts")
            skill_roots.append(self.workspace / ".pi-code-agent/skills")
            prompt_roots.append(self.workspace / ".pi-code-agent/prompts")
        skill_roots.extend(self.workspace / Path(p) for p in self.skill_paths)
        prompt_roots.extend(self.workspace / Path(p) for p in self.prompt_paths)
        seen = set()
        for root in skill_roots:
            try:
                for path in self._skill_candidates(root, seen):
                    try:
                        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8-sig"))
                        name = metadata.get("name") or path.parent.name
                        description = metadata.get("description")
                        if not isinstance(description, str) or not description.strip():
                            raise ValueError("Skill 缺少 description")
                        if not isinstance(name, str) or not name:
                            raise ValueError("Skill 缺少 name")
                        if (
                            len(name) > 64
                            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                            or (path.name == "SKILL.md" and name != path.parent.name)
                            or len(description) > 1024
                        ):
                            diagnostics.append(
                                {
                                    "path": str(path),
                                    "error": "Skill 名称或说明不符合规范；按 Pi 保留可用条目",
                                    "level": "warning",
                                }
                            )
                        if name in skills:
                            diagnostics.append(
                                {
                                    "path": str(path),
                                    "error": "同名 Skill 保留首次来源",
                                    "level": "warning",
                                }
                            )
                        else:
                            skills[name] = Skill(
                                name,
                                description,
                                path.resolve(),
                                metadata.get("disable-model-invocation") is True,
                            )
                    except (OSError, ValueError, yaml.YAMLError) as error:
                        diagnostics.append({"path": str(path), "error": str(error)})
            except OSError as error:
                diagnostics.append({"path": str(root), "error": str(error)})
        for root in prompt_roots:
            paths = sorted(root.glob("*.md")) if root.is_dir() else [root]
            for path in paths:
                if not path.is_file():
                    continue
                try:
                    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8-sig"))
                    description = metadata.get("description") or next(
                        (line[:60] for line in body.splitlines() if line.strip()), ""
                    )
                    templates.setdefault(
                        path.stem, PromptTemplate(path.stem, str(description), body, path.resolve())
                    )
                except (OSError, ValueError, yaml.YAMLError) as error:
                    diagnostics.append({"path": str(path), "error": str(error)})
        self.context_files, self.skills, self.templates = (
            tuple(contexts),
            tuple(skills.values()),
            tuple(templates.values()),
        )
        self.diagnostics = diagnostics
        return self

    def system_prompt(self, base: str) -> str:
        """只加入上下文正文与可由模型选择的 Skill 元数据，不提前加入技能正文。"""
        parts = [base]
        parts.extend(f"\n# {path}\n{body}" for path, body in self.context_files)
        visible = [skill for skill in self.skills if not skill.disable_model_invocation]
        if visible:
            parts.append(
                "需要专门流程时使用 read 读取 skill location；相对引用以 Skill 文件所在目录为基准。\n<available_skills>"
            )
            parts.extend(
                f'<skill name="{escape(s.name, quote=True)}" location="{escape(str(s.path), quote=True)}">{escape(s.description)}</skill>'
                for s in visible
            )
            parts.append("</available_skills>")
        return "\n".join(parts)

    def matches(self, text: str) -> bool:
        name = text.split(" ", 1)[0].removeprefix("/")
        return any(name == p.name for p in self.templates) or any(
            name == "skill:" + s.name for s in self.skills
        )

    def expand(self, text: str) -> str:
        """输入 Hook 后显式展开 Skill 和模板；未知命令原样返回，读取失败明确报错。"""
        name, _, arguments = text.partition(" ")
        if name.startswith("/skill:"):
            skill = next((s for s in self.skills if s.name == name[7:]), None)
            if skill:
                _, body = parse_frontmatter(skill.path.read_text(encoding="utf-8-sig"))
                block = f'<skill name="{escape(skill.name, quote=True)}" location="{escape(str(skill.path), quote=True)}">\n相对引用基于 {skill.path.parent}。\n\n{body.strip()}\n</skill>'
                text = block + ("\n\n" + arguments.strip() if arguments.strip() else "")
        name, _, arguments = text.partition(" ")
        template = next((p for p in self.templates if name == "/" + p.name), None)
        return (
            substitute_args(template.content, parse_command_args(arguments)) if template else text
        )
