"""s21：让检索与基础读取共同面向当前容器，而不是宿主旧源码。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from copy import deepcopy
from pathlib import PurePosixPath

from s12_skill_loading.code import TOOLS
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from s16_repository_search.workspace import SourceSnapshot, source_lines
from s21_sandbox_repair.worker_snapshot import protected_path


def snapshot_fingerprint(snapshot: WorkspaceSnapshot) -> str:
    """对路径和完整字节计算版本，不从工具文字或修改时间推断。"""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.files.items()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


# s21 新增：这是源码视图适配器，不是磁盘路径换个名字；每次读取都来自同一 executor。
class ContainerRepositoryView:
    """向既有检索器提供容器源码，并分发同源基础工具。

    输入：宿主 workspace 布局和隔离 executor；输出：SourceSnapshot、工具目录和只读证据。
    流程：capture_source → 核验文件映射 → scan/读取；写工具委托同一个容器。
    边界：root/index_root 只用于缓存身份和参数边界，不从 root 打开业务源码。
    """

    def __init__(self, *, workspace, executor) -> None:
        """保持目标身份，单独保存容器搜索指纹；不复用宿主的旧 search_hashes。"""
        self.root = workspace.root
        self.workspace_id = workspace.workspace_id
        self.index_root = workspace.index_root
        self.executor = executor
        self.search_hashes = {}
        self.tools = deepcopy(workspace.tools)
        names = {tool["name"] for tool in self.tools}
        self.tools.extend(deepcopy(tool) for tool in TOOLS if tool["name"] not in names)
        for tool in self.tools:
            if tool["name"] == "bash":
                tool["description"] = "在隔离副本 /work 执行 Linux shell；无网络，不在宿主执行。"

    def scan(self) -> SourceSnapshot:
        """固定当前容器允许源码，供全部召回通道共用。

        输入：正在运行的 executor；输出：字节、文件哈希和整体版本。
        流程：导出 source → 再校验路径/类型/文本预算 → 构造标准 SourceSnapshot。
        边界：导出协议损坏属于致命执行器错误，不静默跳过或回退宿主。
        """
        captured = self.executor.capture_source()
        try:
            captured = WorkspaceSnapshot.from_payload(captured.to_payload())
            if any(
                protected_path(path)
                or len(path.encode()) > 512
                or len(data) > 1024 * 1024
                or b"\0" in data
                for path, data in captured.files.items()
            ):
                raise ValueError("容器源码包含禁止路径或非合规文件")
            for data in captured.files.values():
                data.decode("utf-8")
        except (ValueError, TypeError, UnicodeError) as error:
            self.executor.fail(f"容器源码协议无效：{error}")
        hashes = {path: hashlib.sha256(data).hexdigest() for path, data in captured.files.items()}
        return SourceSnapshot(
            file_contents=captured.files,
            file_hashes=hashes,
            skipped_counts={},
            fingerprint=snapshot_fingerprint(captured),
        )

    def compute_fingerprint(self) -> str:
        """再次核对容器视图；IPC 需短暂导出字节，不持久保留第二份源码。"""
        return self.scan().fingerprint

    def permission_hook(self, name: str, arguments: dict) -> str | None:
        """基础工具仅选择容器分发；权限来自隔离，而不是每条宿主审批。"""
        if name not in {tool["name"] for tool in self.tools}:
            return "Permission denied: 未开放的基础工具"
        return None

    def dispatch(self, name: str, arguments: dict) -> str:
        """把六种基础工具绑定同一容器，避免读取宿主、修改副本的错位。

        输入：工具名及参数；输出：工具正文，普通参数错误返回 Error。
        流程：写入/shell 走 executor；读取走当前源码视图；隔离错误继续抛出。
        边界：没有宿主业务 handler，也不接受模型选择其他执行后端。
        """
        if name in {"bash", "write_file", "edit_file"}:
            return self.executor.dispatch(name, arguments)
        handlers = {"read_file": self.read_file, "grep": self.grep, "glob": self.glob}
        if name not in handlers:
            return "Permission denied: 未知基础工具"
        try:
            return handlers[name](**arguments)
        except (TypeError, ValueError) as error:
            return f"Error: {error}"

    def read_file(
        self, path: str, start_line: int = 1, limit: int = 2000, expected_hash: str | None = None
    ) -> str:
        """按容器当前内容补读，不用宿主同名文件解释搜索结果。

        输入：相对路径或 /work 路径、行窗口、可选内容哈希；输出：带行号/哈希的正文。
        流程：导出当前允许源码 → 路径核验 → 核对最近搜索版本 → 按行与字节预算返回。
        边界：旧版本先要求重新检索，不能用过期行号继续；未读完给出续读位置。
        """
        snapshot = self.scan()
        relative = self._path(path, snapshot)
        content_hash = snapshot.file_hashes[relative]
        expected_hash = self.search_hashes.get(relative) if expected_hash is None else expected_hash
        if expected_hash is not None and content_hash != expected_hash:
            raise ValueError("源码版本已变化；请重新 search_code")
        if type(start_line) is not int or type(limit) is not int or min(start_line, limit) < 1:
            raise ValueError("start_line/limit 必须为正整数")
        lines = source_lines(snapshot.file_contents[relative])
        output = [f"{relative} | content_hash={content_hash}"]
        size = len(output[0].encode())
        next_line = start_line
        for number in range(start_line, min(len(lines) + 1, start_line + min(limit, 2000))):
            line = f"{number}: {lines[number - 1]}"
            if size + len(line.encode()) > 50 * 1024 - 256:
                break
            output.append(line)
            size += len(line.encode()) + 1
            next_line = number + 1
        if start_line > len(lines):
            output.append(f"[起始行超过总行数 {len(lines)}]")
        elif next_line == start_line:
            raise ValueError("单行超过读取预算")
        elif next_line <= len(lines):
            output.append(f"[尚有内容；用 start_line={next_line} 继续]")
        return "\n".join(output)

    def grep(self, path: str, pattern: str, limit: int = 50) -> str:
        """只搜索一个容器允许文件；目录搜索交给 search_code。"""
        snapshot = self.scan()
        relative = self._path(path, snapshot)
        if not isinstance(pattern, str) or not pattern or type(limit) is not int or limit < 1:
            raise ValueError("pattern 非空，limit 为正整数")
        matches = []
        size = 0
        for number, line in enumerate(source_lines(snapshot.file_contents[relative]), 1):
            if pattern not in line:
                continue
            start = max(0, line.index(pattern) - 80)
            snippet = line[start : start + 500]
            match = f"{relative}:{number}: {snippet}"
            if len(matches) >= min(limit, 50) or size + len(match.encode()) > 50 * 1024 - 128:
                return "\n".join(matches) + "\n[结果已截断]"
            matches.append(match)
            size += len(match.encode()) + 1
        return "\n".join(matches) or "(no matches)"

    def glob(self, pattern: str) -> str:
        """在容器允许路径中匹配，不枚举宿主目录；最多 100 项。"""
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > 256
            or pattern.startswith("/")
            or ".." in pattern.split("/")
            or "\\" in pattern
        ):
            raise ValueError("glob 需要目标内相对模式")
        names = [name for name in self.scan().file_contents if fnmatch.fnmatchcase(name, pattern)]
        return (
            "\n".join(names[:100]) + ("\n[结果已截断]" if len(names) > 100 else "")
            or "(no matches)"
        )

    def _path(self, value, snapshot):
        """只接受相对路径或容器 /work 下的路径，不解释宿主绝对路径。"""
        if not isinstance(value, str):
            raise TypeError("path 必须是字符串")
        value = value.removeprefix("/work/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or ".." in path.parts
            or "\\" in value
            or ":" in value
            or any(ord(char) < 32 for char in value)
            or protected_path(value)
        ):
            raise ValueError("路径越界或受保护")
        if value not in snapshot.file_contents:
            if any(name.startswith(value + "/") for name in snapshot.file_contents):
                raise ValueError("此工具只读文件；目录搜索请用 search_code")
            raise ValueError("路径不在当前容器允许清单中")
        return value
