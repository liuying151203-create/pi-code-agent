"""s03：保留完整异步循环，新增机制通过明确组件接入。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

# 来自 s01：保持；使用本章错误类型，让终端和外部调用方一致捕获。
from .errors import TurnLimitError
from .messages import AssistantMessage, Message, ToolCall, ToolResultMessage
from .model import Model
from .tools import Tool, execute_tool


# 来自 s01：保持；参考 Pi runLoop，选择其顺序执行模式；并行调度在后续章节加入。
async def agent_loop(
    messages: list[Message],
    *,
    model: Model,
    tools: Sequence[Tool] = (),
    system: str = "你是编程助手，完成任务后停止。",
    max_tokens: int = 8000,
    max_turns: int | None = None,
) -> list[Message]:
    """请求模型、执行工具并回传结果，直到模型不再请求工具。

    输入：可变历史、异步模型和本次工具；输出：原地追加后的同一个历史列表。
    流程：检查依赖 → 请求完整回答 → 记录 → 按停止状态分支 → 执行并回传工具。
    边界：不读配置、不打印、不存会话；模型失败回答保留，工具失败交回模型修正。
    """
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("max_tokens 必须为正整数")
    if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
        raise ValueError("max_turns 必须为正整数或 None")
    catalog = {tool.name: tool for tool in tools}
    if len(catalog) != len(tools):
        raise ValueError("本次工具目录存在重名")
    definitions = [tool.definition() for tool in tools]
    turn = 0
    while max_turns is None or turn < max_turns:
        turn += 1
        response = await model.complete(
            system=system, messages=tuple(messages), tools=definitions, max_tokens=max_tokens
        )
        if not isinstance(response, AssistantMessage):
            raise TypeError("模型必须返回 AssistantMessage")
        calls = [block for block in response.content if isinstance(block, ToolCall)]
        if response.stop_reason not in {"error", "aborted"} and len(
            {call.id for call in calls}
        ) != len(calls):
            raise ValueError("模型返回重复的工具调用 ID，已停止执行")
        messages.append(response)
        # 来自 s01：保持；对齐 Pi，失败/取消回答结束本次运行，不执行其中的工具。
        if response.stop_reason in {"error", "aborted"} or not calls:
            return messages
        for index, call in enumerate(calls):
            # 来自 s01：保持；length 可能包含不完整参数，整批回传错误，让模型重新提出请求。
            if response.stop_reason == "length":
                result = ToolResultMessage(
                    call.id, call.name, "工具调用被截断，请重新完整调用", True
                )
            else:
                try:
                    result = await execute_tool(call, catalog)
                except asyncio.CancelledError:
                    # 来自 s01：保持；传播原生取消，同时补齐未完成调用，避免破坏消息协议。
                    for pending in calls[index:]:
                        messages.append(
                            ToolResultMessage(pending.id, pending.name, "工具执行已取消", True)
                        )
                    raise
            messages.append(result)
    raise TurnLimitError(f"已达到 {max_turns} 轮上限；任务尚未完成")


# s03 新增：入口使用本章冻结的配置和模型组装，明确选择本章的完整循环。
def main(arguments=None) -> int:
    """组装本章的配置、模型与工具，输入终端参数，输出运行退出码。"""
    from .cli import main as cli_main

    # 来自 s03：保持；使用本章无事件终端组装，不引用 s04 或正式 CLI。
    return cli_main(arguments, loop=agent_loop)


if __name__ == "__main__":
    raise SystemExit(main())
