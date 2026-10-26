"""s13-B 新增：共享工作区的启动检查；OS 保护仍由执行后端提供。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pi_code_agent.sandbox_copy.sandbox import SandboxError

PROTECTED_DIRS = {
    ".git",
    ".env",
    ".sessions",
    ".traces",
    ".sandboxes",
    ".sandbox-runtime",
    ".aws",
    ".ssh",
    ".gnupg",
    ".venv",
    "venv",
    "node_modules",
}


@dataclass(frozen=True)
class ProtectedPath:
    """描述一个需要由容器遮蔽或原生策略保护的现有路径。

    输入：宿主路径、相对路径和目录标志；输出：不可变保护项。
    inspect_workspace 创建后，B 用它组装遮蔽物，C 用它生成 OS 策略，不读取正文。
    """

    path: Path
    relative: str
    is_directory: bool


def is_protected_name(name: str) -> bool:
    """匹配凭证、Git 元数据、宿主运行记录和依赖，不把普通缓存当秘密。"""
    name = name.lower()
    return (
        name in PROTECTED_DIRS
        or name in {"id_rsa", "id_ed25519", "credentials"}
        or (name.startswith(".env.") and name != ".env.example")
        or name.endswith((".pem", ".key"))
    )


# s13-B 新增：与 A 的“跳过复制”不同，共享真实目录需要先明确 OS 保护目标。
def inspect_workspace(root: Path) -> tuple[ProtectedPath, ...]:
    """检查准备放权的真实目录，并返回需保护的现有路径。

    输入：规范化项目根目录；输出：按路径排序的保护清单。
    流程：逐层检查普通目录/文件 → 拒绝链接、硬链接和特殊文件 → 收集保护对象。
    边界：不读取秘密正文，不改变宿主权限；保护目录的内部不继续遍历。
    这是启动检查，不是对并发宿主修改的锁；使用期间不要添加凭证或路径别名。
    """
    if root.resolve() != root or not root.is_dir():
        raise SandboxError("工作区已被重定向或不是目录")
    protected = []
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        allowed_dirs = []
        for name in sorted([*dirs, *files]):
            path = parent / name
            count += 1
            if count > 20000:
                raise SandboxError("共享工作区超过 20000 个目录项；请选择更小的项目")
            if path.is_symlink() or path.is_junction():
                raise SandboxError(f"共享工作区不接受链接或 junction：{path}")
            is_directory = path.is_dir()
            if not is_directory and (not path.is_file() or path.stat().st_nlink > 1):
                raise SandboxError(f"共享工作区不接受硬链接或特殊文件：{path}")
            if is_protected_name(name):
                protected.append(
                    ProtectedPath(path, path.relative_to(root).as_posix(), is_directory)
                )
                if len(protected) > 128:
                    raise SandboxError("保护路径超过 128 个；请缩小工作区")
            elif is_directory:
                allowed_dirs.append(name)
        dirs[:] = allowed_dirs
    return tuple(sorted(protected, key=lambda item: item.relative))


def check_file_arguments(name: str, arguments: dict, root: Path) -> str | None:
    """专用文件工具提前拒绝越界及保护路径；不能代替 bash 的 OS 限制。"""
    if name not in {"read_file", "grep", "write_file", "edit_file"}:
        return None
    value = arguments.get("path")
    if not isinstance(value, str):
        return "Permission denied: 文件路径必须是字符串"
    path = Path(value)
    # 容器绝对路径和宿主绝对路径分别由各自入口传入的 root 决定。
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        return "Permission denied: 文件路径超出工作区"
    if any(is_protected_name(part) for part in resolved.relative_to(root).parts):
        return "Permission denied: 不能读取或修改凭证、Git 元数据、运行记录或依赖"
    return None
