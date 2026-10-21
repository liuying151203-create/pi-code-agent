"""s21：容器快照边界；交付快照与检索允许清单分开。仅依赖标准库和 s13-A。"""

import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

from s13_a_sandbox_copy.snapshot import MAX_BYTES, MAX_FILES, WorkspaceSnapshot, excluded_path

# s21 新增：容器没有宿主模块依赖链；显式保留 s16 的检索排除目录，回归测试检查一致性。
EXTRA_DIRS = {
    ".indexes",
    ".evals",
    ".codex",
    ".agents",
    ".mypy_cache",
    ".sourcegraph",
    ".sandbox-runtime",
}


def protected_path(name: str) -> bool:
    """凭证/运行记录/检索缓存不复制、不索引、不交付；不是任意源码秘密识别器。"""
    path = PurePosixPath(name)
    return excluded_path(path) or any(part.lower() in EXTRA_DIRS for part in path.parts)


def capture_project(root: Path) -> WorkspaceSnapshot:
    """采集容器业务文件，不因模型修改 .gitignore 而把原文件误报为删除。

    输入：/work；输出：有界普通文件快照，供交付和验证版本使用。
    流程：固定目录剪枝 → 拒绝文件别名 → 描述符读取 → 文件/字节预算。
    边界：不遵循 Git ignore，避免忽略规则隐藏交付；源码检索另外调用 source_files。
    """
    files, size, entries = {}, 0, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        entries += len(dirs) + len(names)
        if entries > 20000:
            raise ValueError("副本超过 20000 个目录项")
        allowed_dirs = []
        for name in dirs:
            path = parent / name
            if protected_path(path.relative_to(root).as_posix()):
                continue
            # s21 修改：业务目录别名不能静默跳过，否则测的是链接目标，交付却只剩删除。
            if path.is_symlink() or path.is_junction():
                raise ValueError("容器业务目录出现链接，停止导出")
            allowed_dirs.append(name)
        dirs[:] = sorted(allowed_dirs)
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if protected_path(relative):
                continue
            if len(relative.encode()) > 512:
                raise ValueError("容器业务路径超过 512 字节")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path:
                raise ValueError("容器业务文件出现链接或特殊文件，停止导出")
            if len(files) >= MAX_FILES or info.st_size > MAX_BYTES - size:
                raise ValueError("副本超过 4000 文件 / 12 MiB")
            with path.open("rb") as file:
                opened = os.fstat(file.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or path.resolve() != path
                ):
                    raise ValueError("容器文件读取前被替换")
                data = file.read(MAX_BYTES - size + 1)
            size += len(data)
            if size > MAX_BYTES:
                raise ValueError("副本超过 12 MiB")
            files[relative] = data
    return WorkspaceSnapshot(files)


def source_files(
    snapshot: WorkspaceSnapshot, root: Path, *, git_dir: Path = Path("/tmp/zero2pi-s21-ignore")
) -> WorkspaceSnapshot:
    """用真实 Git ignore 过滤当前容器源码，不在 Python 手写不完整的 ignore 解析器。

    输入：业务快照与容器 /work；输出：用于检索/读取的 UTF-8 文件。
    流程：准备 /tmp 内专用 Git 元数据 → check-ignore → 限定文本/单文件预算。
    边界：不初始化业务仓库；不运行过滤器、不联网；Git 故障不静默放行被忽略文件。
    """
    if git_dir.resolve() != git_dir:
        raise ValueError("容器 ignore 元数据被重定向")
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0"
    )
    initialized = subprocess.run(
        ["git", "init", "--bare", "--quiet", str(git_dir)],
        env=environment,
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if initialized.returncode:
        raise ValueError("容器 Git ignore 初始化失败；未继续检索")
    ignored = subprocess.run(
        [
            "git",
            "--git-dir=" + str(git_dir),
            "--work-tree=" + str(root),
            "-c",
            "core.fsmonitor=false",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        input=("\0".join(snapshot.files) + "\0").encode(),
        env=environment,
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if ignored.returncode not in {0, 1}:
        raise ValueError("容器 Git ignore 核对失败")
    excluded = {value.decode() for value in ignored.stdout.split(b"\0") if value}
    files = {}
    for name, data in snapshot.files.items():
        if name in excluded or len(data) > 1024 * 1024 or b"\0" in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeError:
            continue
        files[name] = data
    return WorkspaceSnapshot(files)
