"""s16 新增：有界核对缓存目录，只统计普通独立文件，不读取源码正文。"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

from s16_repository_search.workspace import checked_cache_path

MAX_CACHE_ENTRIES = 20000


# s16 新增：容量观测只检查指定缓存目录，拒绝链接和特殊文件，不扩散到整个项目。
def cache_directory_bytes(directory: Path) -> int:
    """统计一个缓存目录内普通文件的逻辑字节数，不统计文件系统分配块。

    输入：绝对、未重定向的缓存目录；输出：目录内文件大小之和。
    流程：检查根目录 → 有界枚举 → 拒绝链接/硬链接/特殊文件 → 累加大小。
    边界：不读取文件正文、不跨链接、不删除文件；并发改动可导致失败或近似计数。
    """
    return sum(size for _, size in _iter_cache_files(directory))


# s16 新增：容量观测和清理预检使用相同的有界枚举，不分别放宽链接规则。
def _iter_cache_files(directory: Path) -> Iterator[tuple[str, int]]:
    """交出相对文件名和大小；目录、链接和数量检查始终在遍历时执行。"""
    checked_cache_path(directory)
    if not directory.is_dir() or directory.is_junction():
        raise ValueError("缓存路径不是独立目录")
    pending = [directory]
    entries_seen = 0
    while pending:
        current = pending.pop()
        checked_cache_path(current)
        with os.scandir(current) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_CACHE_ENTRIES:
                    raise ValueError("缓存目录项超过 20000 个，停止枚举")
                path = Path(entry.path)
                checked_cache_path(path)
                info = path.lstat()
                if path.is_symlink() or path.is_junction():
                    raise ValueError("缓存中不能包含链接或目录联接")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    yield path.relative_to(directory).as_posix(), info.st_size
                else:
                    raise ValueError("缓存中不能包含硬链接或特殊文件")
