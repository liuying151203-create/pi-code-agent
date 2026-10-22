"""s13-A 新增：宿主确认后交付副本修改；不向模型开放回写工具。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from tutorials.s13_a_sandbox_copy.sandbox import SandboxExecutor
from tutorials.s13_a_sandbox_copy.snapshot import MAX_BYTES, WorkspaceSnapshot


class WorkspaceSyncError(RuntimeError):
    """预览、冲突或交付失败；不能把失败当作应用成功。"""


# ===== s13-A 新增：补丁执行和宿主路径检查 =====


def _git_apply(root: Path, patch: bytes, *, check: bool) -> None:
    """固定 Git argv 和 stdin；不运行 shell、不操作暂存区，也不创建提交。"""
    arguments = ["git", "apply", "--whitespace=nowarn"]
    if check:
        arguments.append("--check")
    arguments.append("-")
    # s13-A 新增：清除可能改变 Git 目标目录的继承变量，不使用用户全局配置。
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        result = subprocess.run(
            arguments,
            input=patch,
            cwd=root,
            env=environment,
            shell=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceSyncError(f"Git 补丁操作失败：{error}") from error
    if result.returncode:
        diagnostic = result.stderr[:2000].decode("utf-8", errors="replace")
        raise WorkspaceSyncError(f"Git {'检查' if check else '应用'}失败：{diagnostic}")


def _validate_file_names(names: set[str]) -> None:
    """限制为 Windows/Linux 均可表达的普通路径，拒绝大小写别名和文件目录转换。"""
    spellings: dict[str, str] = {}
    for name in sorted(names):
        parts = name.split("/")
        for index, part in enumerate(parts):
            if (
                not part
                or part in {".", ".."}
                or any(char in '<>:"|?*\\' or ord(char) < 32 for char in part)
                or part.endswith((".", " "))
                or PureWindowsPath(part).is_reserved()
            ):
                raise WorkspaceSyncError(f"不支持的宿主文件名：{name!r}")
            prefix = "/".join(parts[: index + 1])
            previous = spellings.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise WorkspaceSyncError(f"路径大小写冲突：{previous!r} / {prefix!r}")
            if index < len(parts) - 1 and prefix in names:
                raise WorkspaceSyncError(f"不支持文件与目录互换：{prefix!r}")


def _read_host_files(root: Path, names: tuple[str, ...]) -> dict[str, bytes | None]:
    """按原路径检查每层链接及文件类型，再有界读取；不靠过滤快照掩盖路径冲突。"""
    files: dict[str, bytes | None] = {}
    for name in names:
        target = root
        parts = name.split("/")
        for index, part in enumerate(parts):
            # s13-A 新增：Linux 上同样拒绝大小写别名，保持 Windows 回写语义一致。
            if target.is_dir() and any(
                child.name.casefold() == part.casefold() and child.name != part
                for child in target.iterdir()
            ):
                raise WorkspaceSyncError(f"宿主路径存在大小写别名：{name!r}")
            target = target / part
            if target.is_symlink() or target.is_junction():
                raise WorkspaceSyncError(f"宿主目标或父目录是链接：{name!r}")
            if not target.resolve().is_relative_to(root):
                raise WorkspaceSyncError(f"宿主目标超出项目：{name!r}")
            if target.exists():
                if index < len(parts) - 1 and not target.is_dir():
                    raise WorkspaceSyncError(f"宿主父路径不是目录：{name!r}")
                if index == len(parts) - 1 and (not target.is_file() or target.stat().st_nlink > 1):
                    raise WorkspaceSyncError(f"宿主目标不是独立普通文件：{name!r}")
        if not target.exists():
            files[name] = None
        else:
            with target.open("rb") as file:
                content = file.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise WorkspaceSyncError(f"宿主目标超出读取预算：{name!r}")
            files[name] = content
    return files


# ===== s13-A 新增：预览 → 确认应用 → 更新基线 =====


@dataclass
class WorkspaceSync:
    """管理副本修改的交付边界，不负责模型请求或容器命令。

    输入：实际项目目录、容器执行器、启动基线和固定报告路径。
    输出：当前 changes.diff、确认后的宿主文件修改，以及每次回写的补丁和内容备份。
    流程：preview 保存待确认版本 → main 获取用户确认 → apply 检查并写入 → 更新基线。
    边界：只支持普通非空 UTF-8 文本；冲突整批拒绝，不自动合并、提交或运行宿主代码。
    """

    source_root: Path
    executor: SandboxExecutor
    baseline: WorkspaceSnapshot
    report_path: Path
    changed_files: tuple[str, ...] = field(default=(), init=False)
    _pending: WorkspaceSnapshot | None = field(default=None, init=False, repr=False)
    _patch: bytes = field(default=b"", init=False, repr=False)
    _apply_count: int = field(default=0, init=False)
    _blocked: bool = field(default=False, init=False)

    def preview(self) -> str:
        """获取容器当前版本，计算待应用修改并保存审查报告。

        输入：当前基线及存活容器；输出：Diff 文本，同时更新 changed_files 和待确认版本。
        流程：获取经过校验的快照 → 与基线比较 → 保存报告 → 固定本次预览版本。
        边界：不写项目文件；报告可含不支持的变化，apply 会明确拒绝而非漏应用。
        """
        current = self.executor.capture_workspace()
        names = self.baseline.files.keys() | current.files.keys()
        changed = tuple(
            sorted(
                name for name in names if self.baseline.files.get(name) != current.files.get(name)
            )
        )
        patch = self.baseline.diff(current).encode("utf-8")
        self.report_path.write_bytes(patch)
        # s13-A 新增：保存内容副本，不从可被外部编辑的 changes.diff 读取回写指令。
        self._pending = WorkspaceSnapshot(dict(current.files))
        self.changed_files = changed
        self._patch = patch
        return patch.decode("utf-8")

    def apply(self) -> int:
        """应用已预览且由宿主用户确认的修改，容器继续运行。

        输入：最近一次 preview 的固定版本；输出：应用文件数，成功后更新比较基线。
        流程：复核容器版本 → 校验路径/文本 → 检查宿主前像 → Git 检查 → 备份 →
        再检查前像 → Git 应用 → 核对实际内容 → 更新基线和清空待应用报告。
        边界：本方法不替用户批准；仅 main 的确认分支调用。写入阶段失败禁用后续回写，
        保留补丁和 before-*.tar 供人工检查；不承诺磁盘故障或并发编辑时的完整事务回滚。
        """
        if self._blocked:
            raise WorkspaceSyncError("上次实际回写失败；请先检查补丁、备份和宿主文件，再重新启动")
        if self._pending is None:
            raise WorkspaceSyncError("请先预览修改，再确认应用")
        if not self.changed_files:
            return 0
        # s13-A 新增：确认不能授权预览后产生的新内容；后台进程改变副本也需要重新预览。
        current = self.executor.capture_workspace()
        if current.files != self._pending.files:
            raise WorkspaceSyncError("容器内容已变化；请重新 /diff 预览后再确认 /apply")
        _validate_file_names(set(self.baseline.files) | set(self._pending.files))
        for name in self.changed_files:
            for version in (self.baseline.files, self._pending.files):
                content = version.get(name)
                if content is not None:
                    try:
                        content.decode("utf-8")
                    except UnicodeError as error:
                        raise WorkspaceSyncError(
                            f"不支持二进制变化，整批未应用：{name!r}"
                        ) from error
                    if not content or b"\0" in content:
                        raise WorkspaceSyncError(f"不支持空文件或二进制变化，整批未应用：{name!r}")

        root = self.source_root.resolve()
        # s13-A 新增：根目录本身也不能被替换成指向另一位置的链接。
        if root != self.source_root.absolute():
            raise WorkspaceSyncError("项目根目录已被重定向；禁止回写")
        expected = {name: self.baseline.files.get(name) for name in self.changed_files}
        original = _read_host_files(root, self.changed_files)
        conflicts = [name for name in self.changed_files if original[name] != expected[name]]
        if conflicts:
            raise WorkspaceSyncError(
                f"原仓库目标已变化，整批未应用：{', '.join(repr(name) for name in conflicts)}"
            )
        _git_apply(root, self._patch, check=True)

        # s13-A 新增：写入前保存实际补丁及原内容；不自动覆盖用户同期改动或执行回滚。
        self._apply_count += 1
        run_root = self.report_path.parent
        (run_root / f"apply-{self._apply_count:03d}.diff").write_bytes(self._patch)
        before = WorkspaceSnapshot(
            {name: content for name, content in original.items() if content is not None}
        )
        before.write_tar(run_root / f"before-{self._apply_count:03d}.tar")
        if _read_host_files(root, self.changed_files) != original:
            raise WorkspaceSyncError("确认期间原仓库目标已变化，整批未应用")

        # s13-A 新增：只对普通工作区应用补丁，不使用 --index/--cached/--reject/--unsafe-paths。
        try:
            _git_apply(root, self._patch, check=False)
            actual = _read_host_files(root, self.changed_files)
            desired = {name: self._pending.files.get(name) for name in self.changed_files}
            if actual != desired:
                raise WorkspaceSyncError("回写结果与预览不一致；请检查 apply 补丁和 before 备份")
        except (WorkspaceSyncError, OSError, KeyboardInterrupt):
            self._blocked = True
            raise

        count = len(self.changed_files)
        # s13-A 新增：只在全部成功后推进基线；下一轮 Diff 仅包含尚未交付的修改。
        self.baseline = self._pending
        self._pending = None
        self.changed_files = ()
        self._patch = b""
        try:
            self.report_path.write_bytes(b"")
        except OSError as error:
            raise WorkspaceSyncError(
                "文件已应用且基线已更新，但未能清空报告；请以 apply 补丁和宿主内容为准"
            ) from error
        return count
