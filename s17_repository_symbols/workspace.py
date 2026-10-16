"""s17：复用 s16 的目标读取边界，只为本章绑定独立缓存与记录目录。"""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from s03_permission.code import READ_ONLY_TOOLS
from s05_session_persistence.code import select_session_path
from s16_repository_search.workspace import RepositoryWorkspace as TextWorkspace
from s16_repository_search.workspace import checked_cache_path


# ===== 来自 s16：保持；扫描、过滤、读文件、自动指纹校验与基础工具分发直接复用 =====
# s17 修改：只覆盖目标组装及章节路径，不碰 s16 全局变量或检查 s16 的缓存目录。
class RepositoryWorkspace(TextWorkspace):
    """为符号检索绑定与基础读取完全相同的目标和访问边界。

    输入：目标项目和 Harness 根目录；输出：s17 工作区组件。
    流程：建立目标身份和本章目录 → 组装基础工具 → 复用 s16 的 scan/dispatch/read_file。
    边界：本章不新增基础权限；索引、会话与 Trace 不会与 s16 混用。
    """

    def __init__(self, root: Path, harness_root: Path) -> None:
        """计算目标与目录并组装工具，不扫描源码、不创建文件。

        输入：已有目标目录与 Harness 目录；输出：按章节/项目隔离的路径和工具定义。
        流程：规范化 → 生成项目 ID → 检查记录路径 → 设置只读工具与空指纹表。
        """
        # 来自 s16：保持；相同规范路径产生相同项目 ID，访问规则不变。
        self.root = root.resolve(strict=True)
        harness_root = harness_root.resolve(strict=True)
        if not self.root.is_dir() or self.root == Path(self.root.anchor):
            raise ValueError("目标必须是已有项目目录，不能是磁盘根目录")
        identity = os.path.normcase(str(self.root))
        self.workspace_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
        # s17 修改：仅本章路径改为 s17；不先初始化父类以免依赖无关的 s16 缓存。
        self.index_root = harness_root / ".indexes" / "s17" / self.workspace_id
        self.session_root = harness_root / ".sessions" / "s17" / self.workspace_id
        self.trace_root = harness_root / ".traces" / "s17" / self.workspace_id
        for directory in (self.index_root, self.session_root, self.trace_root):
            checked_cache_path(directory)
        # 来自 s16：保持；指纹仅在查询验证成功且实际返回后登记。
        self.search_hashes: dict[str, str] = {}
        self.tools = deepcopy(READ_ONLY_TOOLS)
        read_tool = next(tool for tool in self.tools if tool["name"] == "read_file")
        read_tool["input_schema"]["properties"]["expected_hash"] = {"type": "string"}
        read_tool["description"] = (
            "读取目标项目 UTF-8 文件，返回行号与内容哈希；最多 2000 行 / 50 KiB。"
            "start_line 从 1 开始；搜索过的文件自动校验指纹，也可显式传 expected_hash。"
        )
        grep_tool = next(tool for tool in self.tools if tool["name"] == "grep")
        grep_tool["description"] = (
            "在单个 UTF-8 文件中搜索字面文本，返回路径与行号；最多 50 条 / 50 KiB。"
            "path 必须是文件；搜索目录请用 search_code(query=文本, scope=相对目录)。"
        )
        grep_tool["input_schema"]["properties"]["limit"]["maximum"] = 50

    # 来自 s16：保持；只调整拒绝提示中的章节标识，权限没有放宽。
    def permission_hook(self, name: str, arguments: dict[str, Any]) -> str | None:
        """基础权限仍由 s16 检查；未知工具的提示使用当前章节名称。"""
        if name not in {"read_file", "grep", "glob"}:
            return "Permission denied: s17 只开放 glob、grep、read_file 和检索扩展"
        return super().permission_hook(name, arguments)

    # 来自 s16：保持；复用相同的项目归属检查，仅更换错误提示。
    def select_session(self, history_path: Path | None) -> Path:
        """选择本章同一目标的会话，不从历史加载权限或搜索指纹。

        输入：可选历史路径；输出：可供 SessionManager 打开的 JSONL 路径。
        流程：检查目录 → 新建名称或验证历史文件归属与普通文件类型。
        """
        checked_cache_path(self.session_root)
        if history_path is None:
            return select_session_path(None, session_root=self.session_root)
        path = history_path.absolute()
        checked_cache_path(path)
        if path.parent != self.session_root or not path.is_file() or path.stat().st_nlink > 1:
            raise ValueError("会话不属于当前 s17 目标项目，不能跨项目加载")
        return path
