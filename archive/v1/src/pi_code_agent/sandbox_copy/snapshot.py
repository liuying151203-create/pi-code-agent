"""s13-A 新增：只保存普通文件的快照，不挂载或自动改写原仓库。"""

from __future__ import annotations

import base64
import difflib
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

# s13-A 新增：限制复制与导出大小，避免源码副本包含运行历史、依赖和常见秘密。
MAX_FILES = 4000
MAX_BYTES = 12 * 1024 * 1024
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".sessions",
    ".traces",
    ".sandboxes",
    ".pytest_cache",
    ".ruff_cache",
    ".aws",
    ".ssh",
    ".gnupg",
    ".idea",
    ".vscode",
    "build",
    "dist",
}


def excluded_path(path: PurePosixPath) -> bool:
    """排除已知运行目录和常见凭证；不承诺识别任意源码中的秘密。"""
    for part in path.parts:
        name = part.lower()
        if name in EXCLUDED_DIRS or name.endswith(".egg-info"):
            return True
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
        if name in {"id_rsa", "id_ed25519", "credentials"} or name.endswith((".pem", ".key")):
            return True
    return False


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """保存项目副本的文件内容，作为复制输入及最终 Diff 的可信基线。

    输入：相对 POSIX 路径到 bytes 的字典；输出：tar、导出 JSON 或审查用 Diff。
    流程：capture 过滤并读取源码 → write_tar 交给容器 → 与容器最终快照比较。
    边界：不复制符号链接、junction、特殊文件，不写回源码；大小超限明确报错。
    """

    files: dict[str, bytes]

    @classmethod
    def capture(cls, root: Path) -> WorkspaceSnapshot:
        """在可信工作区内采集有界普通文件快照。

        输入：源码目录或容器 /work；输出：过滤后的快照。
        流程：逐层剪枝目录 → 跳过链接和特殊文件 → 限制数量与总字节 → 读取。
        边界：排除名单不是通用脱敏；用户仍应只选择可以交给模型的项目。
        """
        root = root.resolve()
        files: dict[str, bytes] = {}
        total = 0
        for directory, dirs, names in os.walk(root, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(
                name
                for name in dirs
                if not excluded_path(PurePosixPath((parent / name).relative_to(root).as_posix()))
                and not (parent / name).is_symlink()
                and not (parent / name).is_junction()
            )
            for name in sorted(names):
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if excluded_path(PurePosixPath(relative)) or path.is_symlink():
                    continue
                # s13-A 新增：硬链接也不复制，避免别名把工作区外的文件带入副本。
                if (
                    not path.is_file()
                    or path.stat().st_nlink > 1
                    or not path.resolve().is_relative_to(root)
                ):
                    continue
                # s13-A 新增：读取前先检查大小；读取时仍限制剩余额度，避免文件变大。
                if len(files) >= MAX_FILES or path.stat().st_size > MAX_BYTES - total:
                    raise ValueError("项目快照超过 4000 文件 / 12 MiB；请使用更小的测试项目")
                with path.open("rb") as file:
                    data = file.read(MAX_BYTES - total + 1)
                total += len(data)
                if total > MAX_BYTES:
                    raise ValueError("项目快照超过 12 MiB")
                files[relative] = data
        return cls(files)

    def write_tar(self, path: Path) -> None:
        """写入只含普通文件的 tar，避免 Docker 复制时跟随宿主链接。"""
        with tarfile.open(path, "w") as archive:
            for name, data in self.files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))

    def to_payload(self) -> dict[str, str]:
        """用 base64 表示文件 bytes，供容器通过 JSON 导出，不在宿主解压 tar。"""
        return {name: base64.b64encode(data).decode("ascii") for name, data in self.files.items()}

    @classmethod
    def from_payload(cls, payload: object) -> WorkspaceSnapshot:
        """校验容器导出数据，不把不可信文件名直接写到宿主。

        输入：容器返回的 JSON 文件映射；输出：有界最终快照。
        流程：校验映射与相对路径 → 严格解码 base64 → 复核文件数和大小。
        边界：只恢复内存数据，输出只用于生成单一 Diff 文件。
        """
        if not isinstance(payload, dict) or len(payload) > MAX_FILES:
            raise ValueError("容器导出不是有效的有界文件映射")
        files: dict[str, bytes] = {}
        total = 0
        for name, encoded in payload.items():
            if not isinstance(name, str) or not name or "\\" in name:
                raise ValueError("容器导出路径无效")
            path = PurePosixPath(name)
            if (
                not path.parts
                or path.is_absolute()
                or PureWindowsPath(name).drive
                or ".." in path.parts
                or path.as_posix() != name
                or any(ord(char) < 32 for char in name)
            ):
                raise ValueError("容器导出路径越界或不规范")
            if excluded_path(path):
                raise ValueError("容器导出包含排除路径")
            if not isinstance(encoded, str):
                raise TypeError("容器导出内容不是 base64 文本")
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if total > MAX_BYTES:
                raise ValueError("容器导出超过 12 MiB")
            files[name] = data
        return cls(files)

    def diff(self, after: WorkspaceSnapshot) -> str:
        """比较新增、修改和删除的文件，生成审查文本，不应用任何变更。

        输入：容器最终快照；输出：文本 unified diff，二进制只报告文件变化。
        流程：比较文件 bytes → 解码 UTF-8 → 生成补丁，保留缺少末尾换行的信息。
        """
        changes: list[str] = []
        for name in sorted(self.files.keys() | after.files.keys()):
            before_data = self.files.get(name, b"")
            after_data = after.files.get(name, b"")
            if name in self.files and name in after.files and before_data == after_data:
                continue
            try:
                before_lines = before_data.decode("utf-8").splitlines(keepends=True)
                after_lines = after_data.decode("utf-8").splitlines(keepends=True)
            except UnicodeError:
                changes.append(f"Binary file changed: {name!r}\n")
                continue
            before_name = f"a/{name}" if name in self.files else "/dev/null"
            after_name = f"b/{name}" if name in after.files else "/dev/null"
            lines = list(difflib.unified_diff(before_lines, after_lines, before_name, after_name))
            if not lines:
                changes.append(f"Empty file added/deleted: {name!r}\n")
            for line in lines:
                changes.append(
                    line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                )
        return "".join(changes)
