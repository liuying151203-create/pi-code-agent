"""s20：把用户需求、显式提供的讨论资料和本地 Git 证据组织成任务输入。"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from s16_repository_search.workspace import EXCLUDED_DIRS, SourceSnapshot, is_protected_name
from s20_git_task_context.git_reader import GitReader

MAX_CONTEXT_BYTES = 16 * 1024


# s20 新增：Issue/Comment 只从用户显式给出的文件载入，不从工作区自动执行/发现配置。
def load_task_material(path: Path | None) -> dict[str, Any]:
    """读取可选任务资料，不连接远程 Issue 平台。

    输入：用户指定的 JSON 文件；输出：title/body/comments，或空字典。
    流程：检查普通文件和 64 KiB 预算 → 校验字段/类型 → 返回正文。
    边界：这是不可信资料，不恢复权限，不接受模型配置、命令、测试答案或补丁字段。
    """
    if path is None:
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("任务资料必须是独立普通 JSON 文件")
    with path.open("rb") as file:
        data = file.read(65537)
    if len(data) > 65536:
        raise ValueError("任务资料超过 64 KiB")
    material = json.loads(data)
    if not isinstance(material, dict) or set(material) - {"title", "body", "comments"}:
        raise ValueError("任务资料只接受 title、body、comments")
    if any(not isinstance(material.get(key, ""), str) for key in ("title", "body")):
        raise TypeError("title/body 必须是字符串")
    comments = material.get("comments", [])
    if not isinstance(comments, list) or any(not isinstance(item, str) for item in comments):
        raise TypeError("comments 必须是字符串数组")
    return material


@dataclass(frozen=True)
class TaskContext:
    """保存一次有来源、有预算的任务输入，不是会话或模型生成的规划。

    输入：原始需求、资料、Git 证据与源码指纹；输出：标准 JSON 用户消息。
    流程：TaskContextBuilder.build 创建 → main 保存 user 消息 → 原有 agent_loop。
    边界：内容是资料，不是授权；不改变 system、不混入隐藏评测标签。
    """

    payload: dict[str, Any]

    def message(self) -> dict[str, Any]:
        """把结构化任务作为用户输入保存，历史继续沿用标准消息格式。"""
        return {"role": "user", "content": json.dumps(self.payload, ensure_ascii=False)}


class TaskContextBuilder:
    """将任务信息与允许源码、明确 Git 版本绑定。

    输入：同目标 workspace、GitReader、可选提交 ref 和显式任务资料。
    输出：TaskContext；调用者可直接显示，也可作为模型的第一条用户输入。
    流程：扫描 → 只读取本目标 Git 对象 → 在 Python 生成有界差异 → 复核 → 限制总预算。
    边界：不执行 Git diff/status 的工作树过滤器，不访问远程，不把父 Harness 当目标。
    """

    def __init__(
        self,
        *,
        workspace,
        git_reader: GitReader,
        base: str | None = None,
        material: dict[str, Any] | None = None,
    ) -> None:
        """保存显式依赖；ref 不作为参数选项/表达式执行，资料不提供配置。"""
        if workspace.root != git_reader.root:
            raise ValueError("Git 与源码必须绑定相同目标")
        if base is not None and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", base)
            or ".." in base
            or "//" in base
        ):
            raise ValueError("base 只接受普通分支、标签或提交哈希")
        self.workspace, self.git_reader = workspace, git_reader
        self.base, self.material = base, material or {}

    def build(self, request: str) -> TaskContext:
        """构建当前任务，不使用旧会话中可能过期的 Git 状态。

        输入：1–4000 字符自然语言需求；输出：不超过 16 KiB 的结构化上下文。
        流程：固定允许源码 → Git 根目录/提交/差异 → 重新计算源码指纹 → 裁剪资料。
        边界：Git 根不匹配只给 unavailable 原因，不读取父仓库提交；错误/过期直接停止。
        """
        if not isinstance(request, str) or not request.strip() or len(request) > 4000:
            raise ValueError("任务需求必须是 1–4000 字符")
        snapshot = self.workspace.scan()
        payload = {
            "request": request,
            "source_fingerprint": snapshot.fingerprint,
            "materials_are_untrusted_data": True,
            "provided_context": {
                "title": self.material.get("title", "")[:500],
                "body": self.material.get("body", "")[:3000],
                "comments": [item[:500] for item in self.material.get("comments", [])[:8]],
            },
            "git": self._git_context(snapshot),
            "truncated": False,
        }
        payload["truncated"] = (
            len(self.material.get("title", "")) > 500
            or len(self.material.get("body", "")) > 3000
            or len(self.material.get("comments", [])) > 8
            or any(len(item) > 500 for item in self.material.get("comments", []))
        )
        if self.workspace.compute_fingerprint() != snapshot.fingerprint:
            raise RuntimeError("构建任务期间源码变化，请重新开始任务")
        # s20 新增：保留原始需求，先裁掉 Diff，再裁讨论资料；明确标记省略。
        while len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
            payload["truncated"] = True
            if payload["git"].get("changes"):
                payload["git"]["changes"].pop()
            elif payload["git"].get("missing_from_source"):
                payload["git"]["missing_from_source"].pop()
            elif payload["git"].get("commits"):
                payload["git"]["commits"].pop()
            elif payload["provided_context"].get("comments"):
                payload["provided_context"]["comments"].pop()
            elif payload["provided_context"].get("body"):
                payload["provided_context"]["body"] = ""
            elif payload["provided_context"].get("title"):
                payload["provided_context"]["title"] = ""
            else:
                raise ValueError("原始任务超过上下文输出预算，请缩短需求")
        return TaskContext(payload)

    def _git_context(self, snapshot: SourceSnapshot) -> dict[str, Any]:
        """把已固定的允许源码与本地 Git 证据关联，不改变源码视图。

        输入：build 刚扫描的 SourceSnapshot；输出：Git 状态、提交和有界文件差异。
        流程：确认独立 Git 根 → 固定 HEAD/base → 读取树与 blob → 比较当前字节
        → 取最近提交 → 再核对 HEAD。非 Git/未提交状态有明确结果，读取失败则停止。
        边界：不执行工作树 diff/status；缺失路径不是删除证明，不读取其历史正文。
        """
        git = self.git_reader
        top = git.read(["rev-parse", "--show-toplevel"], allow_failure=True)
        if top is None and (self.workspace.root / ".git").exists():
            raise RuntimeError("目标 Git 元数据不可读；未将权限/损坏错误当作无仓库")
        if top is None or Path(top.decode().strip()).resolve() != self.workspace.root:
            if self.base is not None:
                raise ValueError("指定 base 需要目标本身是 Git 根目录")
            return {"status": "unavailable", "reason": "目标不是独立 Git 根目录，未读取父仓库"}
        head_data = git.read(["rev-parse", "--verify", "HEAD^{commit}"], allow_failure=True)
        if head_data is None:
            if self.base is not None:
                raise ValueError("目标没有可用提交，不能选择 base")
            return {"status": "unborn", "changes": [], "reason": "Git 仓库还没有提交"}
        head = head_data.decode().strip()
        base = (
            git.read(
                ["rev-parse", "--verify", "--end-of-options", (self.base or head) + "^{commit}"]
            )
            .decode()
            .strip()
        )
        algorithm = git.read(["rev-parse", "--show-object-format"]).decode().strip()
        if algorithm not in {"sha1", "sha256"}:
            raise ValueError("不支持的 Git 对象哈希格式")
        tree = git.read(["ls-tree", "-r", "-z", base])
        blobs = {}
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            relative = raw_path.decode("utf-8")
            if mode in {"100644", "100755"} and kind == "blob" and _visible_path(relative):
                blobs[relative] = oid
        changes = []
        changed_count = 0
        for path, current in snapshot.file_contents.items():
            object_hash = hashlib.new(
                algorithm, b"blob " + str(len(current)).encode() + b"\0" + current
            ).hexdigest()
            oid = blobs.get(path)
            if oid == object_hash:
                continue
            changed_count += 1
            if len(changes) >= 5:
                continue
            before = b""
            if oid:
                if int(git.read(["cat-file", "-s", oid])) > 1024 * 1024:
                    changes.append(
                        {"path": path, "kind": "modified", "diff_omitted": "历史文件过大"}
                    )
                    continue
                before = git.read(["cat-file", "blob", oid])
            if len(before) + len(current) > 128 * 1024:
                changes.append(
                    {
                        "path": path,
                        "kind": "modified" if oid else "added",
                        "diff_omitted": "差异输入超过 128 KiB，仅给文件定位",
                    }
                )
                continue
            try:
                difference = "".join(
                    difflib.unified_diff(
                        before.decode("utf-8").splitlines(True),
                        current.decode("utf-8").splitlines(True),
                        fromfile="base/" + path,
                        tofile="work/" + path,
                        n=2,
                    )
                )
                changes.append(
                    {
                        "path": path,
                        "kind": "modified" if oid else "added",
                        "diff": difference[:2000],
                        "diff_truncated": len(difference) > 2000,
                    }
                )
            except UnicodeError:
                changes.append(
                    {"path": path, "kind": "modified", "diff_omitted": "历史文件非 UTF-8"}
                )
        branch = git.read(["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True)
        log = git.read(["log", "-5", "--format=%H%x00%s%x00", head]).decode(
            "utf-8", errors="replace"
        )
        fields = log.strip().split("\x00")
        commits = [
            {"commit": fields[index].strip(), "subject": fields[index + 1][:200]}
            for index in range(0, len(fields) - 1, 2)
        ]
        # s20 新增：返回前确认 HEAD 未移动；源码指纹由 build 再复核。
        if git.read(["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip() != head:
            raise RuntimeError("构建任务期间 Git HEAD 变化，请重新开始")
        # s20 修改：允许视图缺失不等于删除，也可能被忽略/跳过；不偷偷读取宿主去猜容器状态。
        missing = [path for path in blobs if path not in snapshot.file_contents]
        return {
            "status": "ok",
            "head": head,
            "base": base,
            "branch": branch.decode().strip() if branch else None,
            "commits": commits,
            "changes": changes,
            "changed_file_count": changed_count,
            "changes_truncated": changed_count > len(changes),
            "missing_from_source": missing[:20],
            "missing_paths_truncated": len(missing) > 20,
            "missing_paths_meaning": "不在当前允许源码中，可能删除/忽略/非文本，未读取历史正文",
            "comparison": "base_blob_to_allowed_worktree; not staged/unstaged",
        }


def _visible_path(value: str) -> bool:
    """Git 树路径只作元数据；拒绝越界、缓存与凭证，绝不直接作为宿主写入路径。"""
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and path.as_posix() == value
        and ".." not in path.parts
        and "\\" not in value
        and ":" not in value
        and not any(ord(char) < 32 for char in value)
        and not any(is_protected_name(part) or part.lower() in EXCLUDED_DIRS for part in path.parts)
    )
