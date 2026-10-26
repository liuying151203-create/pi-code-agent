"""绑定单个工作区的工具；不修改教程的模块全局工作目录。"""

from __future__ import annotations

import glob
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

# s02 修改：实例后端与教程共用 Pi 风格编辑，路径及权限仍由本实例检查。
from pi_code_agent.editing import edit_text_file
from pi_code_agent.permissions import TOOLS, confirm_permission
from pi_code_agent.sandbox_shared.workspace_access import is_protected_name
from pi_code_agent.tools import _limit_shell_output, _read_file_window, _search_file


class LocalTools:
    """给每个 Agent 绑定独立路径、目录和权限。

    输入：已有工作区和模式；输出：tools/dispatch/permission_hook。
    流程：绑定根目录 → 检查真实操作 → 调用有界工具。
    边界：不是 OS 沙箱，宿主 shell/写入逐次确认；放权应显式选择沙箱后端。
    """

    def __init__(self, workspace: Path, mode: str = "read-only") -> None:
        self.root = Path(workspace).resolve(strict=True)
        self.protected_roots: tuple[Path, ...] = ()
        if not self.root.is_dir() or self.root == Path(self.root.anchor):
            raise ValueError("工作区必须是已有目录，不能是磁盘根目录")
        if mode not in {"read-only", "default"}:
            raise ValueError("本地工具模式必须是 read-only 或 default")
        self.tools = deepcopy(
            [
                tool
                for tool in TOOLS
                if mode == "default" or tool["name"] in {"read_file", "grep", "glob"}
            ]
        )
        self.handlers = {
            "read_file": self.read_file,
            "grep": self.grep,
            "glob": self.glob,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "bash": self.bash,
        }

    def _path(self, path: str) -> Path:
        """解析后再检查工作区、凭证与运行目录边界。"""
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径超出工作区")
        if any(resolved.is_relative_to(root) for root in self.protected_roots):
            raise ValueError("不开放当前运行记录目录")
        parts = resolved.relative_to(self.root).parts
        if any(is_protected_name(part) or part == ".pi-code-agent" for part in parts):
            raise ValueError("不开放凭证、索引或运行记录路径")
        return resolved

    def permission_hook(self, name: str, arguments: dict[str, Any]) -> str | None:
        """核对开放工具和路径，宿主写入及 shell 不能隐式放权。"""
        if name not in {tool["name"] for tool in self.tools}:
            return f"Permission denied: 当前模式未开放 {name}"
        if name in {"read_file", "grep", "write_file", "edit_file"}:
            try:
                self._path(arguments.get("path", ""))
            except (TypeError, ValueError) as error:
                return f"Permission denied: {error}"
        if name in {"bash", "write_file", "edit_file"} and not confirm_permission(
            name, arguments, "宿主操作可能修改项目或访问外部资源"
        ):
            return "Permission denied: user did not approve this tool call"
        return None

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """按实例注册表分发；ExtensionAPI 在调用前执行统一门禁。"""
        handler = self.handlers.get(name)
        if handler is None:
            return f"Error: unknown tool {name}"
        try:
            return handler(**arguments)
        except (TypeError, ValueError, OSError, UnicodeError) as error:
            return f"Error: {error}"

    def read_file(self, path: str, limit: int | None = None, start_line: int = 1) -> str:
        """复用 s02 的行数、字节预算和继续位置。"""
        return _read_file_window(self._path(path), start_line, limit)

    def grep(self, path: str, pattern: str, limit: int = 100) -> str:
        """只搜索文件，目录检索交给业务扩展。"""
        return _search_file(self._path(path), pattern, limit)

    def glob(self, pattern: str) -> str:
        """过滤越界、敏感目录，最多返回 200 项。"""
        found = []
        for name in sorted(glob.glob(pattern, root_dir=self.root, recursive=True)):
            try:
                self._path(name)
            except ValueError:
                continue
            found.append(name)
        return "\n".join(found[:200]) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        """写入已校验的路径。"""
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"

    # s02 修改：整批定位、唯一匹配和格式保留同步用于正式 SDK。
    def edit_file(
        self,
        path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        *,
        edits: list[dict[str, str]] | None = None,
    ) -> str:
        """校验工作区路径后应用整批替换；输出成功文本，失败交给 dispatch 转成错误。"""
        target = self._path(path)
        edit_text_file(target, old_text, new_text, edits=edits)
        return f"Edited {path}"

    def bash(self, command: str) -> str:
        """在宿主工作区运行 shell，权限入口已要求逐次确认。"""
        # s02 修改：先校验模型参数类型，不能把 JSON 数组当作子进程 argv 执行。
        if not isinstance(command, str):
            return "Error: command must be a string"
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 120 seconds"
        output = _limit_shell_output((result.stdout + result.stderr).strip(), self.root)
        return (
            output
            if not result.returncode
            else f"Error: command exited with code {result.returncode}\n{output}"
        )
