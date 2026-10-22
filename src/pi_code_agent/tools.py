#!/usr/bin/env python3
"""s02：工具调用与工具分发。

本章保留 s01 的核心循环，只把“执行一个 bash 命令”扩展为：

    tool_name + tool_arguments -> TOOL_HANDLERS -> tool_result

模型负责选择工具和生成参数，Harness 负责通过注册表找到处理函数并执行。
"""

from __future__ import annotations

import glob as glob_module
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

# ===== 来自 s01：消息类型与模型调用类型 =====
# s02 保留 s01 的消息结构，并新增工具处理函数和分发器类型。

Message = dict[str, Any]
ToolHandler = Callable[..., str]
DispatchTool = Callable[[str, dict[str, Any]], str]

WORKDIR = Path.cwd()
# s02 修改：参考 Pi，工具输出预算独立于会话压缩，避免小窗口导致大量补读。
TOOL_MAX_LINES = 2000
TOOL_MAX_BYTES = 50 * 1024
GREP_MAX_MATCHES = 100
GREP_MAX_LINE_CHARS = 500

# ===== s02 修改：全局系统提示词 =====
# 在 s01 的全局提示词基础上，增加“优先使用专用工具”的约束。

SYSTEM = (
    f"你是运行在 {WORKDIR} 的编程 Agent。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。Windows 下使用 cmd.exe 命令。"
)


# ===== 来自 s01：bash 工具（保持） =====


def run_bash(command: str) -> str:
    """执行 shell 命令并返回有界的完成反馈。

    输入：命令文本；输出：末尾结果、可选存档路径或包含退出码的错误文本。
    流程：在 WORKDIR 执行 → 合并标准输出与错误 → 截取/存档 → 报告退出状态。
    边界：超时 120 秒；预算限制回传内容，不是子进程沙箱或完整输出内存限制。
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except OSError as error:
        return f"Error: {error}"

    output = (result.stdout + result.stderr).strip()
    # s02 修改：参考 Pi 保留 shell 末尾结果，全文另存；退出失败不能伪装成成功。
    bounded_output = _limit_shell_output(output, WORKDIR)
    if result.returncode:
        # s02 修改：状态放在截断之后，超长错误输出也不会丢失退出码。
        return f"Error: command exited with code {result.returncode}\n{bounded_output}"
    return bounded_output


# s02 新增：输出截取是底层工具边界；不把存档与字节计算混入上层循环。
def _limit_shell_output(output: str, workspace: Path) -> str:
    """截取 shell 输出末尾；超限全文保存到忽略 Git 的工具结果目录。"""
    if not output:
        return "(no output)"
    lines = output.splitlines()
    if len(lines) <= TOOL_MAX_LINES and len(output.encode("utf-8")) <= TOOL_MAX_BYTES:
        return output
    try:
        archive = workspace / ".sessions" / "tool-results" / f"{uuid4().hex}.txt"
        # s02 修改：运行目录若被链接到工作区外，也不能借输出存档越界写文件。
        if not archive.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("工具结果目录超出工作区范围")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(output, encoding="utf-8")
    except (OSError, ValueError) as error:
        return f"Error: 无法保存完整工具结果：{error}"
    tail = "\n".join(lines[-TOOL_MAX_LINES:]).encode("utf-8")
    shown = tail[-TOOL_MAX_BYTES:].decode("utf-8", errors="ignore")
    return shown + f"\n\n[显示末尾输出，可能从行中开始；完整结果：{archive.as_posix()}。]"


# ===== s02 新增：工作区路径与文件工具 =====


def safe_path(path: str) -> Path:
    """把相对路径解析到工作目录内，拒绝越界路径。"""
    # s02 新增：文件工具统一限制在工作区，避免路径参数直接访问外部文件。
    resolved = (WORKDIR / path).resolve()
    if not resolved.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_read(
    path: str,
    limit: int | None = None,
    start_line: int = 1,
) -> str:
    """读取工作区中的有界 UTF-8 代码窗口。

    输入：文件路径、一基起始行、可选行数。
    输出：完整行正文与准确的继续位置；越界或单行超限返回错误。
    流程：检查工作区路径 → 按行数和字节预算读取 → 返回正文，不改写文件。
    """
    try:
        # s02 修改：预算在实际读取边界强制执行，不依赖模型是否主动传 limit。
        return _read_file_window(safe_path(path), start_line, limit)
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error: {error}"


# s02 新增：底层窗口接收已校验路径，供 s03 在自己的工作区边界内复用。
def _read_file_window(path: Path, start_line: int, limit: int | None) -> str:
    """对已校验路径按完整行截取；预算以 UTF-8 字节计，不截断中文字符。"""
    if start_line < 1 or (limit is not None and limit < 1):
        raise ValueError("start_line 和 limit 必须为正整数")
    lines = path.read_text(encoding="utf-8").splitlines()
    if start_line > len(lines):
        return f"(start_line {start_line} exceeds file length {len(lines)})"
    line_limit = min(limit if limit is not None else TOOL_MAX_LINES, TOOL_MAX_LINES)
    selected: list[str] = []
    size = 0
    for line in lines[start_line - 1 : start_line - 1 + line_limit]:
        line_size = len(line.encode("utf-8")) + bool(selected)
        if size + line_size > TOOL_MAX_BYTES:
            break
        selected.append(line)
        size += line_size
    if not selected:
        return f"Error: 第 {start_line} 行超过字节预算，无法按完整行读取；请说明此限制。"
    output = "\n".join(selected)
    next_line = start_line + len(selected)
    if next_line <= len(lines):
        output += (
            f"\n\n[已显示第 {start_line}-{next_line - 1} 行，共 {len(lines)} 行；"
            f"需要更多内容时用 read_file(start_line={next_line}, limit={TOOL_MAX_LINES})。]"
        )
    return output


# s02 新增：参考 Pi 的 grep 返回路径与行号；仅搜索指定文件，先解决当前方法定位闭环。
def run_grep(path: str, pattern: str, limit: int = GREP_MAX_MATCHES) -> str:
    """在指定 UTF-8 文件中搜索字面文本，不执行 shell。

    输入：工作区文件、非空搜索文本与匹配上限。
    输出：文件路径、行号和有界匹配行；无匹配或参数错误时明确说明。
    流程：校验路径 → 逐行匹配 → 限制匹配数、单行长度和字节数 → 返回定位证据。
    边界：与 Pi 不同，暂不递归目录或解析正则；用 glob 选择文件，再用 read_file 补读。
    """
    try:
        return _search_file(safe_path(path), pattern, limit)
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error: {error}"


# s02 新增：逐行搜索与大小控制留在底层，避免为代码定位启动 shell。
def _search_file(path: Path, pattern: str, limit: int) -> str:
    """在已校验的文件中匹配字面文本，并限制返回大小。"""
    if not pattern or limit < 1:
        raise ValueError("pattern 不能为空，limit 必须为正整数")
    shown: list[str] = []
    size = 0
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            if pattern not in line:
                continue
            text = line.rstrip("\r\n")
            if len(text) > GREP_MAX_LINE_CHARS:
                text = text[:GREP_MAX_LINE_CHARS] + " [匹配行已截断，用 read_file 补读]"
            match = f"{path.as_posix()}:{line_number}: {text}"
            match_size = len(match.encode("utf-8")) + bool(shown)
            if len(shown) >= min(limit, GREP_MAX_MATCHES) or size + match_size > TOOL_MAX_BYTES:
                return "\n".join(shown) + "\n[匹配结果已达上限；请缩小搜索文本或文件范围。]"
            shown.append(match)
            size += match_size
    return "\n".join(shown) if shown else "(no matches)"


def run_write(path: str, content: str) -> str:
    """向工作目录内的文件写入文本，必要时创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"
    except (OSError, ValueError) as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中替换第一次出现的精确文本。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error: {error}"


def run_glob(pattern: str) -> str:
    """查找工作目录内匹配 glob 模式的文件，并返回相对路径。"""
    try:
        matches = sorted(
            {
                match
                for match in glob_module.glob(pattern, root_dir=WORKDIR, recursive=True)
                if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
            }
        )
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except (OSError, ValueError) as error:
        return f"Error: {error}"


# ===== s02 新增：工具定义 =====

# s02 新增：把 s01 的单个 bash 工具扩展为文件操作、路径查找与内容定位。
TOOLS = [
    {
        "name": "bash",
        "description": "执行工作区 shell 命令；Windows 使用 cmd.exe。返回末尾最多 2000 行 / 50 KiB，超长全文另存。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "读取工作区 UTF-8 文件，最多 2000 行 / 50 KiB。"
            "用 start_line（一基行号）和 limit 读取目标范围；按返回的继续位置补读。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        # s02 新增：工具定义直接说明搜索边界，不靠增长系统提示词纠正 shell 定位。
        "name": "grep",
        "description": "在指定 UTF-8 文件搜索字面文本，返回路径与行号；最多 100 个匹配，每行最多 500 字符。定位后用 read_file 补读，不递归目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "pattern"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text to a file in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact text occurrence in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find workspace files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]

# ===== s02 新增：工具注册表 =====

# s02 新增：用名称到处理函数的映射替代核心循环中的工具分支，便于继续扩展工具。
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": run_bash,
    "read_file": run_read,
    # s02 新增：内容搜索与其他工具走相同分发链，不改变核心循环。
    "grep": run_grep,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ===== s02 新增：统一工具分发 =====


def dispatch_tool(name: str, arguments: dict[str, Any]) -> str:
    """根据工具名查找并执行对应的工具处理函数。

    作用：把模型返回的工具名称和参数转换成统一的 handler 调用，隔离核心循环
    与具体工具实现。

    输入：
        name：模型返回的工具名称。
        arguments：模型生成的 JSON 参数字典。

    输出：
        工具处理函数返回的结果文本；未知工具、参数错误或运行异常都会转换为
        可回传给模型的错误文本。

    流程：查找注册表 → 调用 handler → 捕获工具边界内的异常 → 返回统一文本结果。
    """
    # s02 新增：统一从注册表查找 handler，让 agent_loop 不依赖具体工具实现。
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool: {name}"

    try:
        return handler(**arguments)
    except TypeError as error:
        return f"Error: invalid arguments for {name}: {error}"
    except Exception as error:  # noqa: BLE001 - 工具错误要回传给模型，而不是打断循环
        return f"Error running {name}: {error}"


# ===== 来自 s01：响应读取辅助（保持） =====


def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ===== 来自 s01：核心循环；s02 修改工具执行入口 =====
# 与 lcc 只把 bash 替换为查表调用的写法一致；s02 额外保留 dispatch 注入，便于测试和复用。


# ===== 来自 s01：终端输出辅助（保持） =====


def _text_from_content(content: Any) -> str:
    """提取模型响应中的文本块，用于终端展示。"""
    if isinstance(content, str):
        return content
    return "\n".join(
        str(text)
        for block in content
        if _get(block, "type") == "text"
        if (text := _get(block, "text"))
    )


# ===== 来自 s01：交互入口；s02 修改工具配置 =====
