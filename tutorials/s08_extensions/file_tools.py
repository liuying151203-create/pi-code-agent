# ===== s08 快照 =====
# 来自 s06：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s02：本章实际使用的只读文本工具，执行边界绑定到实例工作区。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .tools import Tool, ToolResult


# 来自 s02：保持；对齐 Pi read 的一基 offset/limit，schema 与运行校验来自同一声明。
class ReadArguments(BaseModel):
    """声明只读文件参数，行数上限与字节上限由处理函数实际执行。"""

    path: str = Field(min_length=1, description="工作区内的文本文件")
    offset: int = Field(default=1, ge=1, description="从 1 开始的行号")
    limit: int = Field(default=2000, ge=1, le=2000)


def _read_window(path: Path, offset: int, limit: int) -> str:
    """按完整行读取最多 50 KiB，超限给出下一次 offset，不截断 UTF-8 字符。"""
    selected = []
    size = 0
    more = False
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if number < offset:
                continue
            value = line.rstrip("\r\n")
            length = len(value.encode("utf-8")) + bool(selected)
            if len(selected) >= limit or size + length > 50 * 1024:
                more = True
                break
            selected.append(value)
            size += length
    if more and not selected:
        raise ValueError("单行超过 50 KiB，当前文本工具无法完整返回该行")
    if not selected:
        return "（文件为空或 offset 超出文件范围）"
    output = "\n".join(selected)
    if more:
        output += f"\n\n[输出已截断；继续读取请使用 offset={offset + len(selected)}]"
    return output


def read_tool(workspace: Path) -> Tool:
    """创建绑定工作区的只读工具，不放宽文件系统写入或 shell 权限。

    输入：已有项目目录；输出：可供循环与 SDK 使用的 read 工具。
    流程：绑定真实根路径 → 每次解析目标 → 检查边界 → 异步读取有界文本。
    边界：拒绝越界、Git 元数据和 .env；这是应用路径边界，不是 OS 沙箱。
    """
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace 必须是已有目录")

    # 来自 s02：保持；处理函数有独立读文件职责，闭包只捕获本工具的工作区实例。
    async def execute(arguments: dict[str, Any]) -> ToolResult:
        target = (root / arguments["path"]).resolve()
        if not target.is_relative_to(root):
            raise ValueError("文件路径超出工作区")
        for part in target.relative_to(root).parts:
            name = part.lower()
            if (
                name
                in {".git", ".sessions", ".traces", ".indexes", ".pi-code-agent", ".aws", ".ssh"}
                or name == ".env"
                or (name.startswith(".env.") and name != ".env.example")
            ):
                raise ValueError("不开放 Git 元数据或模型配置文件")
        content = await asyncio.to_thread(
            _read_window, target, arguments["offset"], arguments["limit"]
        )
        return ToolResult(content)

    return Tool(
        "read", "读取工作区内的 UTF-8 文本；默认最多 2000 行或 50 KiB", execute, ReadArguments
    )
