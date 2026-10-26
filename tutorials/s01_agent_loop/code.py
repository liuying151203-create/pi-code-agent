#!/usr/bin/env python3
"""s01：最小可用的 Agent 核心循环。

循环流程为：用户消息 → 模型 → tool_use → 执行工具 → tool_result → 模型。
模型负责决定是否调用工具，Harness 负责执行工具并把结果放回消息历史，
直到模型返回不包含工具调用的最终回答。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from pi_code_agent.model import ModelRequester
from pi_code_agent.ui import (
    format_assistant_message,
    format_error,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# ===== s01 基础：消息与依赖类型 =====

Message = dict[str, Any]
CreateMessage = Callable[..., Any]
# s01 修改：从第一章就用工具名与参数对象作为执行契约，与正式 SDK 一致。
DispatchTool = Callable[[str, dict[str, Any]], str]

# ===== s01 新增：全局系统提示词 =====

SYSTEM = (
    f"你是运行在 {os.getcwd()} 的编程 Agent。"
    "只有任务需要操作工作区时才使用 bash；问候和简单问题直接回答。"
    "使用最少行动，完成后停止。Windows 下使用 cmd.exe 命令。"
)

# ===== s01 新增：最小 bash 工具定义 =====

TOOLS = [
    {
        "name": "bash",
        "description": "Run one shell command in the current project directory. On Windows, use cmd.exe syntax.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
]


# ===== s01 新增：工具执行 =====


def run_bash(command: str, *, cwd: str | None = None) -> str:
    """执行一条 shell 命令并返回标准输出和错误输出。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or os.getcwd(),
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

    output = (result.stdout + result.stderr).strip() or "(no output)"
    # s01 修改：参考 Pi bash，非零退出码必须作为工具失败报告。
    if result.returncode:
        return f"Error: command exited with code {result.returncode}\n{output}"
    return output


# s01 新增：终端 bash 工具提到顶层，避免 main() 同时承载工具实现。
def execute_bash(name: str, arguments: dict[str, Any]) -> str:
    """接收工具名与参数，显示并执行本章唯一的 bash；返回结果文本。"""
    # s01 修改：名称由循环核对；实际参数在执行前检查，避免错误参数启动子进程。
    if name != "bash":
        raise ValueError(f"unknown tool: {name}")
    if set(arguments) != {"command"} or not isinstance(arguments["command"], str):
        raise ValueError("bash 需要字符串 command")
    command = arguments["command"]
    print(format_tool_call("bash", {"command": command}), flush=True)
    output = run_bash(command)
    print(format_tool_result(output), flush=True)
    return output


# ===== s01 基础：响应读取辅助 =====


def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ===== s01 新增：核心循环 =====
# 与 lcc 的最小示例相比，本实现把模型请求和工具执行作为参数注入，便于测试和后续替换。


def agent_loop(
    messages: list[Message],
    *,
    create_message: CreateMessage,
    dispatch: DispatchTool,
    system: str,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行 Agent 的最小核心循环。

    作用：让模型能够看到工具执行结果，并在需要时继续推理，而不是在
    第一次返回工具调用后就结束。这个函数只负责循环和消息历史，不负责
    具体的模型实现或工具实现，二者通过参数注入，便于替换和测试。

    输入：
        messages：已有的对话消息列表，会在原列表上追加 assistant 和 tool_result 消息。
        create_message：创建一次模型响应的函数，接收 system、messages、tools 和 max_tokens。
        dispatch：接收工具名和参数对象的函数；本章只处理 bash，s02 增加注册表。
        system：发送给模型的系统提示词。
        tools：提供给模型的工具定义；未传入时使用本章唯一的 bash 工具。
        max_tokens：单次模型响应的最大 token 数。

    输出：
        返回追加了完整执行过程的同一个消息列表。当模型响应中不再包含
        tool_use 时停止循环，此时最后一条 assistant 消息就是最终回答。

    流程：
        1. 请求模型并追加 assistant 消息。
        2. 检查响应中是否包含 tool_use。
        3. 没有工具调用则结束；有则逐个执行并收集 tool_result。
        4. 将工具结果作为 user 消息追加，回到第 1 步。
    """
    # s01 修改：None 才使用默认目录，显式空列表不会扩大能力。
    active_tools = TOOLS if tools is None else tools
    while True:
        response = create_message(
            system=system,
            messages=messages,
            tools=active_tools,
            max_tokens=max_tokens,
        )
        messages.append({"role": "assistant", "content": response.content})
        # s01 修改：参考 Pi runLoop，模型失败时停止，不执行失败回答里的工具。
        if _get(response, "stop_reason") in {"error", "aborted"}:
            raise RuntimeError("模型返回失败或取消，已停止任务")
        tool_calls = [block for block in response.content if _get(block, "type") == "tool_use"]
        if not tool_calls:
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            name = _get(block, "name")
            arguments = _get(block, "input")
            if arguments is None:
                arguments = {}
            # s01 修改：对齐 Pi 的声明查找、错误回传及不执行截断调用的边界。
            if _get(response, "stop_reason") in {"max_tokens", "length"}:
                output = "Error: tool call was truncated; retry with a complete call"
            elif name not in {tool["name"] for tool in active_tools}:
                output = f"Error: unknown tool: {name}"
            elif not isinstance(arguments, dict):
                output = "Error: tool arguments must be an object"
            else:
                try:
                    output = dispatch(name, arguments)
                except Exception as error:  # noqa: BLE001 - 对齐 Pi 的普通工具失败回传边界
                    output = f"Error: {error}"
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                    "is_error": output.startswith("Error:"),
                }
            )
        messages.append({"role": "user", "content": results})


# ===== s01 基础：终端输出 =====


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


# ===== s01 新增：交互入口 =====


def main() -> None:
    """启动第一章的交互式终端 Agent。

    作用：读取本地 `.env` 中的模型配置，创建 Anthropic 客户端，接收用户
    输入并交给 `agent_loop`。本方法只负责终端交互和依赖组装，核心循环
    本身保持在 `agent_loop` 中，方便后续章节继续复用和扩展。

    输入：用户在终端输入的自然语言任务。
    输出：模型最终返回的文本；输入空行、`q` 或 `exit` 时退出程序。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("Set MODEL_ID in .env before running tutorials.s01_agent_loop.")

    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))

    client_options: dict[str, Any] = {
        "timeout": timeout_seconds,
        "max_retries": max_retries,
    }
    if api_key := os.getenv("ANTHROPIC_API_KEY"):
        client_options["api_key"] = api_key
    if base_url := os.getenv("ANTHROPIC_BASE_URL"):
        client_options["base_url"] = base_url
    client = Anthropic(**client_options)
    # s01 修改：使用顶层 ModelRequester，入口中的模型调用关系更直接。
    requester = ModelRequester(client, model, timeout_seconds)

    print("s01：核心循环")
    print("输入任务，输入 q 退出。\n")

    history: list[Message] = []
    while True:
        try:
            query = input(format_user_prompt("s01")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        history.append({"role": "user", "content": query})
        try:
            # s01 修改：终端注入带展示的执行器，SDK 可注入无展示的同契约后端。
            agent_loop(
                history,
                create_message=requester,
                dispatch=execute_bash,
                system=SYSTEM,
            )
        except RuntimeError as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print(format_assistant_message(_text_from_content(history[-1]["content"])))
        print()


if __name__ == "__main__":
    main()
