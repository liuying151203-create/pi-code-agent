"""s13-A 新增：绑定目标工作区和宿主记录，不改变会话 JSONL 格式。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from s05_session_persistence import code as s05


# s13-A 新增：路径选择有独立职责，核心循环不需要知道记录目录或工作区归属。
@dataclass(frozen=True)
class WorkspaceLayout:
    """确定一次运行的目标项目，以及该项目在 Harness 中的记录位置。

    输入：目标目录与 Harness 目录；输出：源码路径、项目 ID 和三类记录目录。
    流程：for_path 规范化路径并分组 → select_session_path 新建绑定或校验历史。
    边界：只负责路径与会话归属，不复制源码、不创建容器、不执行模型请求；
    绑定文件是防误用的宿主记录，不是针对恶意修改日志的认证机制。
    """

    source_root: Path
    workspace_id: str
    session_root: Path
    trace_root: Path
    sandbox_root: Path

    @classmethod
    def for_path(
        cls, workspace: Path, harness_root: Path, *, chapter: str = "s13-a"
    ) -> WorkspaceLayout:
        """为现有项目计算稳定的记录位置，不创建目录或文件。

        输入：相对启动目录的目标路径、Harness 根目录、沙箱版本；输出：路径布局。
        流程：校验目标目录 → 规范化绝对路径 → 名称加路径哈希 → 检查记录边界。
        同一实际路径复用分组，同名但位于不同位置的项目不会共用记录。
        """
        source_root = workspace.resolve(strict=True)
        harness_root = harness_root.resolve(strict=True)
        # s13-B/C 新增：三个实际运行入口按版本再分组，不能跨沙箱版本恢复授权或会话。
        if chapter not in {"s13-a", "s13-b", "s13-c"}:
            raise ValueError("工作区记录版本必须是 s13-a、s13-b 或 s13-c")
        if not source_root.is_dir() or source_root == Path(source_root.anchor):
            raise ValueError("工作区必须是已有项目目录，不能是文件或磁盘根目录")
        # s13-A 新增：Windows 大小写路径归为同一项目；哈希不依赖 Python 随机 hash。
        identity = os.path.normcase(str(source_root))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        label = re.sub(r"[^a-z0-9_-]+", "-", source_root.name.lower()).strip("-")[:40]
        workspace_id = f"{label or 'workspace'}-{digest}"
        # s13-B/C 修改：版本成为目录的一层；默认 A 的路径和既有会话绑定保持不变。
        session_root = harness_root / ".sessions" / chapter / workspace_id
        trace_root = harness_root / ".traces" / chapter / workspace_id
        sandbox_root = harness_root / ".sandboxes" / chapter / workspace_id
        # s13-A 新增：记录目录不能通过预置链接转向目标项目或其他宿主位置。
        for path in (session_root, trace_root, sandbox_root):
            if path.resolve() != path or not path.is_relative_to(harness_root):
                raise ValueError(f"运行记录目录不能通过链接重定向：{path}")
        return cls(source_root, workspace_id, session_root, trace_root, sandbox_root)

    def select_session_path(self, history_path: Path | None) -> Path:
        """新建带工作区绑定的会话，或在加载前核对历史归属。

        输入：可选历史 JSONL 路径；输出：可供既有 SessionManager 打开的文件。
        流程：新建时复用 s05 路径选择并写旁边的 .workspace.json；恢复时检查目录、
        文件类型和绑定内容，全部匹配才返回；不改消息、压缩和用量格式。
        边界：旧记录缺少绑定时拒绝加载，不自动猜测、迁移或覆盖它们。
        """
        if self.session_root.resolve() != self.session_root:
            raise ValueError("会话目录已被重定向，禁止写入或加载")
        if history_path is None:
            # 来自 s05：保持；新文件名称和碰撞处理沿用已有实现。
            path = s05.select_session_path(None, session_root=self.session_root)
            binding = {"workspace": os.path.normcase(str(self.source_root)), "session": path.name}
            # s13-A 新增：绑定放在宿主旁路文件，不把路径元数据混进模型上下文。
            with path.with_suffix(".workspace.json").open("x", encoding="utf-8") as file:
                json.dump(binding, file, ensure_ascii=False)
            return path

        if history_path.is_symlink():
            raise ValueError("不能通过链接加载会话")
        path = history_path.resolve(strict=True)
        if path.parent != self.session_root or not path.is_file() or path.stat().st_nlink > 1:
            raise ValueError("历史会话不属于当前工作区的记录目录，禁止跨项目加载")
        binding_path = path.with_suffix(".workspace.json")
        if (
            not binding_path.is_file()
            or binding_path.is_symlink()
            or binding_path.stat().st_nlink > 1
        ):
            raise ValueError("历史会话缺少有效工作区绑定；旧记录保留但不能自动恢复")
        # s13-A 新增：只接受当前会话的绑定，不把其他仓库拷来的 JSONL 直接接入。
        with binding_path.open("rb") as file:
            data = file.read(16385)
        if len(data) > 16384:
            raise ValueError("会话工作区绑定文件过大")
        try:
            binding = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ValueError("会话工作区绑定文件无效") from error
        expected = {"workspace": os.path.normcase(str(self.source_root)), "session": path.name}
        if binding != expected:
            raise ValueError("历史会话工作区绑定不匹配，禁止跨项目加载")
        return path
