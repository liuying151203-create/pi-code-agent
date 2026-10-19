"""s16 新增：检索和基础读取共用一个目标、过滤规则与源码版本。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from s03_permission.code import READ_ONLY_TOOLS
from s05_session_persistence.code import select_session_path
from s13_b_sandbox_workspace.workspace_access import is_protected_name

# ===== s16 新增：过滤规则与快照数据 =====
# s16 新增：容量限制在实际扫描/读取时检查，不依赖模型传入的小参数。
MAX_FILE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_FILES = 5000
MAX_ENTRIES = 20000
EXCLUDED_DIRS = {
    ".indexes",
    ".evals",
    ".codex",
    ".agents",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "build",
    "dist",
    ".sourcegraph",
}


def checked_cache_path(path: Path) -> Path:
    """拒绝专用缓存或运行记录路径被链接重定向。"""
    if path.resolve() != path:
        raise ValueError("缓存或运行记录目录不能通过链接重定向")
    return path


def source_lines(data: bytes) -> list[str]:
    """与 Zoekt 一致只按 LF 计行；CRLF 去掉行尾 CR，不把 Unicode 分隔符当新行。"""
    lines = data.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


# s16 新增：字段直接说明所存数据；源码快照在内存中，不是磁盘副本或会话消息。
@dataclass(frozen=True)
class SourceSnapshot:
    """在内存中保存一次扫描读到的合规源码，以及这些源码的版本。

    输入：相对路径到原始字节的映射、内容哈希、跳过计数和整体指纹。
    输出：交给 RepositoryIndex 建索引，交给 CodeRetriever 复核结果。
    流程：workspace.scan 创建 → index_manager.prepare 将字节写入过滤副本并建索引
    → CodeRetriever 用同一份字节核验匹配，再次扫描比较整体指纹。
    边界：文件系统扫描不是原子快照；本章检测可见变化，不锁定外部编辑器。
    """

    file_contents: dict[str, bytes]  # s16 新增：相对路径 → 完整原始字节，不只是文件名。
    file_hashes: dict[str, str]  # s16 新增：相对路径 → 单文件哈希，用于 read_file 版本校验。
    skipped_counts: dict[str, int]  # s16 新增：按原因统计跳过数量，不保留被排除文件正文。
    fingerprint: str  # s16 新增：整个允许范围的内容指纹，用于判断索引能否复用。


# ===== s16 新增：目标绑定与基础只读工具 =====
# s16 新增：借鉴 s13 的目标绑定；不修改只支持 A/B/C 的 WorkspaceLayout。
class RepositoryWorkspace:
    """绑定目标仓库、目标内索引缓存和 Harness 内的运行记录。

    输入：已有目标目录、Harness 根目录；输出：扫描、只读分发和会话路径入口。
    流程：规范化目标 → 启动时验证缓存可写 → 枚举并过滤 → 返回源码或快照。
    边界：业务源码只读，Harness 可写专用缓存；不执行项目或提供 OS 沙箱。
    """

    def __init__(self, root: Path, harness_root: Path) -> None:
        """计算路径与项目身份，初始化空的搜索指纹记录，不创建目录或索引。

        输入：目标和 Harness 路径；输出：独立工作区组件。
        流程：拒绝磁盘根目录 → 用规范路径哈希分组 → 绑定三类记录位置。
        """
        self.root = root.resolve(strict=True)
        harness_root = harness_root.resolve(strict=True)
        if not self.root.is_dir() or self.root == Path(self.root.anchor):
            raise ValueError("目标必须是已有项目目录，不能是磁盘根目录")
        identity = os.path.normcase(str(self.root))
        self.workspace_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
        # s16 修改：缓存跟随实际目标；项目由目录隔离，代次仍包含目标身份。
        self.index_root = self.root / ".indexes" / "s16"
        self.session_root = harness_root / ".sessions" / "s16" / self.workspace_id
        self.trace_root = harness_root / ".traces" / "s16" / self.workspace_id
        for directory in (self.index_root, self.session_root, self.trace_root):
            checked_cache_path(directory)
        # s16 新增：只记住成功返回过的文件指纹；这是补读依据，不是权限清单或源码缓存。
        self.search_hashes: dict[str, str] = {}
        # s16 修改：相对 s15，只开放同一目标内的三种基础只读工具，不加载 Todo/Skills。
        self.tools = deepcopy(READ_ONLY_TOOLS)
        read_tool = next(tool for tool in self.tools if tool["name"] == "read_file")
        read_tool["input_schema"]["properties"]["expected_hash"] = {"type": "string"}
        read_tool["description"] = (
            "读取目标项目 UTF-8 文件，返回行号与内容哈希；最多 2000 行 / 50 KiB。"
            "start_line 从 1 开始；搜索过的文件自动校验指纹，也可显式传 expected_hash。"
        )
        # s16 修改：本章只搜索单文件且最多 50 条，定义不再沿用 s03 的 100 条说明。
        grep_tool = next(tool for tool in self.tools if tool["name"] == "grep")
        grep_tool["description"] = (
            "在单个 UTF-8 文件中搜索字面文本，返回路径与行号；最多 50 条 / 50 KiB。"
            "path 必须是文件；搜索目录请用 search_code(query=文本, scope=相对目录)。"
        )
        grep_tool["input_schema"]["properties"]["limit"]["maximum"] = 50

    # s16 新增：启动时实际写入验证，而不是用可读权限推断缓存也可写。
    def prepare_index_directory(self) -> None:
        """创建并验证目标内的索引目录，不扫描源码或构建索引。

        输入：已绑定的 index_root；输出：可写的缓存目录，验证文件自动清除。
        流程：拒绝链接重定向 → 创建目录 → 写入并刷新临时文件 → 关闭并删除。
        失败时明确停止，不切换到 Harness 或其他目录；业务源码不被改写。
        """
        try:
            checked_cache_path(self.index_root)
            self.index_root.mkdir(parents=True, exist_ok=True)
            checked_cache_path(self.index_root)
            with tempfile.TemporaryFile(dir=self.index_root) as probe:
                probe.write(b"zero2pi cache write check\n")
                probe.flush()
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"无法创建或写入目标项目的索引目录：{self.index_root}；"
                "请检查目录写入权限和路径，未切换到其他缓存位置。"
            ) from error

    def scan(self) -> SourceSnapshot:
        """读取本次允许索引的文件，未提交修改同样计入版本。

        输入：绑定的目标及固定容量策略；输出：SourceSnapshot。
        流程：列出合规文件 → 应用 Git ignore → 读取 UTF-8 字节 → 生成逐文件和整体哈希。
        超出总容量直接失败；二进制、超大单文件和已排除路径只计数，不伪造完整覆盖。
        """
        skipped_counts: dict[str, int] = {}
        file_contents: dict[str, bytes] = {}
        file_hashes: dict[str, str] = {}
        # s16 修改：安全读取和容量检查共用迭代器；首次扫描才积攒全部正文。
        for relative, data in self._iter_source_files(skipped_counts):
            file_contents[relative] = data
            file_hashes[relative] = hashlib.sha256(data).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(file_hashes, sort_keys=True).encode()).hexdigest()
        # s16 新增：用字段名组装，让源码字节、文件哈希与整体指纹的去向就地可读。
        return SourceSnapshot(
            file_contents=file_contents,
            file_hashes=file_hashes,
            skipped_counts=skipped_counts,
            fingerprint=fingerprint,
        )

    # s16 新增：复核只需要版本，避免再次构造含全部正文的 SourceSnapshot。
    def compute_fingerprint(self) -> str:
        """重新读取允许范围并计算指纹，不积攒源码正文。

        输入：绑定的目标和当前过滤规则；输出：与 scan().fingerprint 同口径的字符串。
        流程：逐文件安全读取 → 计算文件哈希 → 按路径排序计算整体指纹。
        边界：仍完整读取允许文件，保持新增/删除、Git ignore 和容量检查；不锁定文件系统。
        """
        file_hashes: dict[str, str] = {}
        for relative, data in self._iter_source_files({}):
            file_hashes[relative] = hashlib.sha256(data).hexdigest()
        return hashlib.sha256(json.dumps(file_hashes, sort_keys=True).encode()).hexdigest()

    def permission_hook(self, name: str, arguments: dict[str, Any]) -> str | None:
        """允许本章基础只读工具，拒绝 shell、写入和未知基础工具。

        输入：真实基础工具名和参数；输出：None 或拒绝原因。
        流程：检查名称/参数 → 轻量检查路径边界和文件/目录类型。
        s16 修改：不在这里枚举整个仓库；完整允许清单由实际 handler 在读取前核对。
        扩展 search_code 自己通过同一个 workspace 扫描，不借旧章的全局权限放行。
        """
        if name not in {"read_file", "grep", "glob"}:
            return "Permission denied: s16 只开放 glob、grep、read_file 和检索扩展"
        if not isinstance(arguments, dict):
            return "Error: 工具参数必须是对象"
        if name in {"read_file", "grep"}:
            try:
                # s16 修改：前置检查不生成完整清单，避免注册层和分发层各扫描一遍。
                relative = self._relative_path(arguments.get("path"))
                if (self.root / relative).is_dir():
                    # s16 修改：路径安全后再识别目录误用，不把参数错误说成权限不足。
                    if name == "grep":
                        scope = "" if relative == "." else relative
                        return (
                            "Error: grep 只能搜索单个文件；搜索目录请用 "
                            f"search_code(query=搜索文本, scope={scope!r})"
                        )
                    return "Error: read_file 只能读取单个文件；列出文件请用 glob"
            except PermissionError as error:
                return f"Permission denied: {error}"
            except (ValueError, OSError) as error:
                return f"Error: {error}"
        return None

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """分发目标绑定的基础工具，不调用 s03 的全局 handler。

        输入：工具名和参数；输出：有界正文或明确错误。
        流程：检查权限 → 按名称调用 read_file/grep/glob → 将参数及读取错误返回模型。
        """
        reason = self.permission_hook(name, arguments)
        if reason is not None:
            return reason
        handlers = {"read_file": self.read_file, "grep": self.grep, "glob": self.glob}
        try:
            return handlers[name](**arguments)
        # s16 修改：最终清单拒绝仍是安全拒绝，不能因前置检查变轻而放行忽略文件。
        except PermissionError as error:
            return f"Permission denied: {error}"
        except (TypeError, ValueError, OSError, UnicodeError) as error:
            return f"Error: {error}"

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        limit: int = 2000,
        expected_hash: str | None = None,
    ) -> str:
        """补读检索命中的源码，可显式拒绝旧行号对应的新版本。

        输入：路径、行窗口、可选预期哈希，以及本进程已登记的搜索指纹。
        输出：当前哈希和带行号的完整行；版本变化时拒绝返回新版正文。
        流程：核对一次当前允许清单 → 有界读取 → 显式或自动指纹校验 → 按预算返回。
        边界：未搜索过的文件仍可直接读取；搜索指纹不替代权限校验，也不从会话恢复。
        """
        relative = self._relative_file(path)
        data = self._read_bytes(relative)
        if b"\x00" in data:
            raise ValueError("不读取二进制文件")
        content_hash = hashlib.sha256(data).hexdigest()
        # s16 新增：模型省略参数时使用最近成功搜索的指纹；显式值仍按调用者要求校验。
        if expected_hash is None:
            expected_hash = self.search_hashes.get(relative)
        if expected_hash is not None and expected_hash != content_hash:
            raise ValueError("源码版本已变化；请重新 search_code，不沿用旧行号")
        if type(start_line) is not int or type(limit) is not int or min(start_line, limit) < 1:
            raise ValueError("start_line 和 limit 必须为正整数")
        lines = source_lines(data)
        output = [f"{relative} | content_hash={content_hash}"]
        size = len(output[0].encode())
        next_line = start_line
        for number in range(start_line, min(len(lines) + 1, start_line + min(limit, 2000))):
            text = f"{number}: {lines[number - 1]}"
            # s16 新增：为继续位置预留字节，正文加提示也不能超过声明的预算。
            if size + len(text.encode()) + 1 > 50 * 1024 - 256:
                break
            output.append(text)
            size += len(text.encode()) + 1
            next_line = number + 1
        if start_line > len(lines):
            output.append(f"[起始行超过文件总行数 {len(lines)}]")
        elif next_line == start_line:
            raise ValueError("目标单行超过输出预算，无法按完整行读取")
        elif next_line <= len(lines):
            output.append(f"[尚有内容；用 read_file(start_line={next_line}) 继续]")
        return "\n".join(output)

    def grep(self, path: str, pattern: str, limit: int = 50) -> str:
        """在一个允许读取的文件中作字面搜索，返回最多 50 条有界匹配行。"""
        relative = self._relative_file(path)
        if not isinstance(pattern, str) or not pattern or type(limit) is not int or limit < 1:
            raise ValueError("pattern 不能为空，limit 必须为正整数")
        data = self._read_bytes(relative)
        if b"\x00" in data:
            raise ValueError("不读取二进制文件")
        matches = []
        size = 0
        for number, line in enumerate(source_lines(data), 1):
            if pattern in line:
                text = line if len(line) <= 500 else line[:500] + " [行已截断]"
                match = f"{relative}:{number}: {text}"
                if (
                    len(matches) >= min(limit, 50)
                    or size + len(match.encode()) + 1 > 50 * 1024 - 128
                ):
                    return "\n".join(matches) + "\n[结果已截断，请缩小范围]"
                matches.append(match)
                size += len(match.encode()) + 1
        return "\n".join(matches) or "(no matches)"

    def glob(self, pattern: str) -> str:
        """只列出允许访问的文件路径；不跟随链接，最多返回 200 个路径。"""
        if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
            raise ValueError("pattern 必须是非空短字符串")
        pattern = pattern.replace("\\", "/")
        if pattern.startswith("/") or ":" in pattern or ".." in pattern.split("/"):
            raise ValueError("glob 必须使用工作区内的相对模式")
        paths, _ = self._eligible_paths()
        matches = [path for path in paths if fnmatch.fnmatchcase(path, pattern)]
        # s16 新增：**/ 也匹配根目录文件，避免用户只看到子目录而漏掉入口。
        if pattern.startswith("**/"):
            matches = sorted(
                set(matches) | {p for p in paths if fnmatch.fnmatchcase(p, pattern[3:])}
            )
        output = "\n".join(matches[:200]) or "(no matches)"
        if len(matches) > 200:
            output += "\n[路径已截断，请缩小模式]"
        return output

    def select_session(self, history_path: Path | None) -> Path:
        """只在当前项目分组内新建或恢复会话，不从历史恢复权限。

        输入：可选 JSONL 路径；输出：可供 s15 SessionManager 打开的路径。
        流程：检查目录未重定向 → 恢复时检查归属/普通文件 → 新建时复用 s05 命名。
        """
        checked_cache_path(self.session_root)
        if history_path is None:
            return select_session_path(None, session_root=self.session_root)
        path = history_path.absolute()
        checked_cache_path(path)
        if path.parent != self.session_root or not path.is_file() or path.stat().st_nlink > 1:
            raise ValueError("会话不属于当前 s16 目标项目，不能跨项目加载")
        return path

    # ===== s16 新增：底层路径、文件清单与读取检查 =====
    # s16 新增：这些检查由扫描与读取共用，避免两条路径开放不同文件。
    # s16 修改：首次快照和末尾指纹复核共用过滤、读取与总容量检查，不分叉规则。
    def _iter_source_files(self, skipped_counts: dict[str, int]) -> Iterator[tuple[str, bytes]]:
        """逐个交出合规路径和字节，并向调用方提供的字典累计跳过原因。"""
        paths, excluded = self._eligible_paths()
        skipped_counts.update(excluded)
        total_bytes = 0
        for relative in paths:
            try:
                data = self._read_bytes(relative)
                data.decode("utf-8")
            except UnicodeError:
                skipped_counts["non_utf8"] = skipped_counts.get("non_utf8", 0) + 1
                continue
            if b"\x00" in data:
                skipped_counts["binary"] = skipped_counts.get("binary", 0) + 1
                continue
            total_bytes += len(data)
            if total_bytes > MAX_SOURCE_BYTES:
                raise ValueError("允许索引的源码超过 32 MiB；请指定更小的目标目录")
            yield relative, data

    # s16 新增：边界检查不扫描仓库，供前置权限和最终读取共同使用。
    def _relative_path(self, value: Any) -> str:
        """规范化目标内路径，拒绝越界、保护名称及链接重定向，不判断清单成员。"""
        if not isinstance(value, str) or not value:
            raise ValueError("path 必须是非空字符串")
        path = (self.root / value).absolute()
        if not path.is_relative_to(self.root) or ".." in path.parts:
            raise PermissionError("路径超出目标工作区")
        relative_path = path.relative_to(self.root)
        if any(
            is_protected_name(part) or part.lower() in EXCLUDED_DIRS for part in relative_path.parts
        ):
            raise PermissionError("不能读取凭证、运行记录、依赖或其他保护路径")
        if self.root.resolve() != self.root or not self.root.is_dir() or path.resolve() != path:
            raise PermissionError("目标工作区或文件路径已重定向或消失")
        return relative_path.as_posix()

    # s16 修改：完整文件清单只在实际读取入口核对一次；每次调用都重新核对，不长期缓存。
    def _relative_file(self, value: Any) -> str:
        """检查当前允许文件；直接调用 read_file/grep 也必须经过该检查。"""
        relative = self._relative_path(value)
        if (self.root / relative).is_dir():
            raise ValueError("path 必须指向单个文件；搜索目录请用 search_code，列出文件请用 glob")
        paths, _ = self._eligible_paths()
        if relative not in paths:
            raise PermissionError("路径不在允许读取的文件清单中")
        return relative

    def _eligible_paths(self) -> tuple[list[str], dict[str, int]]:
        """枚举普通文件后批量应用 Git ignore；只返回汇总跳过原因。"""
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise ValueError("目标工作区已重定向或消失")
        paths: list[str] = []
        skipped: dict[str, int] = {}
        count = 0
        for directory, dirs, files in os.walk(
            self.root, followlinks=False, onerror=_raise_walk_error
        ):
            allowed_dirs = []
            for name in sorted([*dirs, *files]):
                path = Path(directory) / name
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError("目录项超过 20000 个；请指定更小的项目")
                info = path.lstat()
                if path.is_symlink() or path.is_junction():
                    reason = "link"
                elif is_protected_name(name) or name.lower() in EXCLUDED_DIRS:
                    reason = "protected"
                elif stat.S_ISDIR(info.st_mode):
                    allowed_dirs.append(name)
                    continue
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    reason = "special_or_hardlink"
                elif info.st_size > MAX_FILE_BYTES:
                    reason = "large_file"
                else:
                    relative = path.relative_to(self.root).as_posix()
                    if len(relative.encode()) > 512:
                        raise ValueError("文件相对路径超过 512 字节")
                    paths.append(relative)
                    continue
                skipped[reason] = skipped.get(reason, 0) + 1
            dirs[:] = allowed_dirs
        # s16 新增：Git ignore 在读正文之前生效；不把未跟踪文件等同于忽略文件。
        ignored = self._git_ignored(paths)
        if ignored:
            skipped["gitignored"] = len(ignored)
        paths = sorted(set(paths) - ignored)
        if len(paths) > MAX_FILES:
            raise ValueError("可索引文件超过 5000 个；请指定更小的项目")
        return paths, skipped

    def _git_ignored(self, paths: list[str]) -> set[str]:
        """复用 Git 的 ignore 语义，不自写不完整的 .gitignore 解析器。"""
        command = ["git", "-c", "core.fsmonitor=false", "-C", str(self.root)]
        try:
            probe = subprocess.run(
                command + ["rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            if any((parent / ".git").exists() for parent in (self.root, *self.root.parents)):
                raise ValueError("Git 工作区需要本机 Git，不能静默忽略过滤规则") from None
            return set()
        if probe.returncode != 0:
            if b"not a git repository" in probe.stderr:
                return set()
            raise ValueError("无法核对 Git 工作区，未继续索引")
        if not paths:
            return set()
        result = subprocess.run(
            command + ["check-ignore", "--no-index", "-z", "--stdin"],
            input="\x00".join(paths).encode("utf-8") + b"\x00",
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise ValueError("Git ignore 检查失败，未继续索引")
        return {p.decode("utf-8") for p in result.stdout.split(b"\x00") if p}

    def _read_bytes(self, relative: str) -> bytes:
        """读取时重新拒绝路径别名，并对实际文件描述符检查类型和大小。"""
        path = self.root / relative
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise ValueError("源码路径已重定向")
        before = path.stat()
        with path.open("rb") as file:
            current = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink > 1
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ValueError("源码文件已变化或不是普通独立文件")
            data = file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("源码读取时超过 1 MiB，未继续")
        if path.resolve() != path:
            raise ValueError("源码读取期间路径已重定向")
        return data


def _raise_walk_error(error: OSError) -> None:
    """扫描失败不能静默漏掉无法访问的子目录。"""
    raise error
