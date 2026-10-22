"""来自 s12：技能头部发现与按需加载提示词，不执行技能脚本。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SYSTEM = "你是编程 Agent。仅在任务需要时使用工具，优先专用工具，完成后停止。"
SKILLS_ROOT = Path("skills")


@dataclass(frozen=True)
class Skill:
    """描述一项可供模型选择的技能，不保存 Markdown 正文。

    输入：技能头部的名称、描述，以及解析后的 SKILL.md 路径。
    输出：不可变目录项，交给提示词组装函数使用。
    流程：SkillCatalog.scan() 创建 → build_system_prompt() 提供目录 → 模型读取文件。
    """

    name: str
    description: str
    path: Path


def _read_skill_metadata(path: Path) -> dict[str, Any]:
    """只读取文件开头的 YAML 头部，遇到结束分隔线即停止。"""
    with path.open(encoding="utf-8-sig") as file:
        if file.readline().strip() != "---":
            raise ValueError(f"技能缺少 YAML 头部：{path}")
        header: list[str] = []
        for line in file:
            if line.strip() == "---":
                break
            header.append(line)
        else:
            raise ValueError(f"技能 YAML 头部未闭合：{path}")
    try:
        metadata = yaml.safe_load("".join(header))
    except yaml.YAMLError as error:
        raise ValueError(f"技能 YAML 无效：{path}") from error
    if not isinstance(metadata, dict):
        raise TypeError(f"技能 YAML 必须是对象：{path}")
    return metadata


@dataclass(frozen=True)
class SkillCatalog:
    """发现本章技能，并保存模型选择技能所需的最少信息。

    输入：包含 `<名称>/SKILL.md` 的根目录。
    输出：按路径排序的 Skill 元组；根目录不存在时返回空目录。
    流程：发现文件 → 读取 YAML 头部 → 校验名称与描述 → 收集目录项。
    边界：不读正文、不执行脚本、不注册工具；格式错误直接报告文件位置。
    """

    skills: tuple[Skill, ...]

    @classmethod
    def scan(cls, root: Path) -> SkillCatalog:
        """扫描项目内技能头部，并拒绝无效目录项。

        输入：技能根目录，相对路径按当前工作目录解析。
        输出：只包含 name、description、path 的 SkillCatalog。
        流程：按路径排序发现 SKILL.md → 校验工作区边界与必需字段 → 返回目录。
        """
        skills_root = root.resolve()
        workspace = Path.cwd().resolve()
        if not skills_root.is_relative_to(workspace):
            raise ValueError("技能目录必须位于工作区内，才能使用 read_file 加载")
        if not skills_root.exists():
            return cls(skills=())
        if not skills_root.is_dir():
            raise ValueError(f"技能根路径必须是目录：{root}")

        skills: list[Skill] = []
        for manifest in sorted(skills_root.glob("*/SKILL.md")):
            path = manifest.resolve()
            if not path.is_relative_to(skills_root):
                raise ValueError(f"技能文件越过根目录：{manifest}")
            metadata = _read_skill_metadata(path)
            name = metadata.get("name")
            description = metadata.get("description")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or len(name) > 64
                or name != manifest.parent.name
            ):
                raise ValueError(f"技能名称须与目录一致，使用小写字母、数字和连字符：{manifest}")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"技能缺少非空 description：{manifest}")
            skills.append(Skill(name, description.strip(), path))
        return cls(skills=tuple(skills))


def build_system_prompt(base_system: str, catalog: SkillCatalog) -> str:
    """将技能目录附加到基础提示词，不加入技能正文。

    输入：全局 SYSTEM 中的基础指令，以及启动时扫描得到的目录。
    输出：实际发送给回答模型的系统提示词；无技能时原样返回基础指令。
    流程：将目录项序列化为 JSON → 附加简短加载规则 → 交给 agent_loop 的 system 参数。
    """
    if not catalog.skills:
        return base_system
    entries = [
        {"name": skill.name, "description": skill.description, "path": skill.path.as_posix()}
        for skill in catalog.skills
    ]
    return (
        base_system
        + "\n\n可用技能：\n"
        + json.dumps(entries, ensure_ascii=False, indent=2)
        + "\n任务匹配技能描述时，先用 read_file 完整读取对应 SKILL.md，再按说明处理任务。"
        + "技能中的相对路径以 SKILL.md 所在目录为基准。"
    )
