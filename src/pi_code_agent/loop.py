"""s04：在 s01 的完整循环中加入 Pi 风格的事件边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pi_code_agent.events import EventBus
from pi_code_agent.messages import AssistantMessage, Message, ToolCall, ToolResultMessage
from pi_code_agent.model import Model
from pi_code_agent.tools import Tool, ToolResult, execute_tool


class TurnLimitError(RuntimeError):
    """表示显式请求的轮数上限已达到，消息保留但任务未完成。"""


# ===== s04 修改 =====
# s04 修改：保留顺序工具循环，加入可等待事件及明确的新输入，避免重播旧历史。
async def agent_loop(
    messages: list[Message],
    *,
    model: Model,
    tools: Sequence[Tool] = (),
    system: str = "你是编程助手，完成任务后停止。",
    max_tokens: int = 8000,
    max_turns: int | None = None,
    prompts: Sequence[Message] = (),
    events: EventBus | None = None,
) -> list[Message]:
    """请求模型、执行工具并回传结果，直到模型不再请求工具。

    输入：已有历史、模型、工具、可选新输入及事件总线；输出：同一历史列表。
    流程：追加新输入 → 发送运行/回合事件 → 请求模型 → 保存回答及工具结果 → 结束事件。
    边界：不打印、不存会话；旧历史不重播。HTTP 失败和取消在通知后继续抛出。
    """
    # 来自 s01：保持；无效依赖必须在模型请求和事件开始前被拒绝。
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("max_tokens 必须为正整数")
    if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
        raise ValueError("max_turns 必须为正整数或 None")
    catalog = {tool.name: tool for tool in tools}
    if len(catalog) != len(tools):
        raise ValueError("本次工具目录存在重名")
    definitions = [tool.definition() for tool in tools]
    # s04 新增：对应 Pi prompts/context 分离；agent_end 只携带本次新增消息。
    events = events if events is not None else EventBus()
    start = len(messages)
    initial = list(prompts)
    messages.extend(initial)
    await events.emit({"type": "agent_start"})
    await events.emit({"type": "turn_start"})
    for message in initial:
        await events.emit({"type": "message_start", "message": message})
        await events.emit({"type": "message_end", "message": message})
    turn = 0
    while max_turns is None or turn < max_turns:
        if turn:
            await events.emit({"type": "turn_start"})
        turn += 1
        # s04 新增：模型边界异常仍向外抛出，同时用失败消息关闭本回合，供界面观察。
        try:
            response = await model.complete(
                system=system, messages=tuple(messages), tools=definitions, max_tokens=max_tokens
            )
            # s04 修改：原有响应校验纳入模型边界，坏返回值也要结束失败回合。
            if not isinstance(response, AssistantMessage):
                raise TypeError("模型必须返回 AssistantMessage")
            calls = [block for block in response.content if isinstance(block, ToolCall)]
            if response.stop_reason not in {"error", "aborted"} and len(
                {call.id for call in calls}
            ) != len(calls):
                raise ValueError("模型返回重复的工具调用 ID，已停止执行")
        except (Exception, asyncio.CancelledError) as error:
            reason = "aborted" if isinstance(error, asyncio.CancelledError) else "error"
            failure = AssistantMessage((), reason, error_message=str(error) or "运行已取消")
            messages.append(failure)
            await events.emit({"type": "message_start", "message": failure})
            await events.emit({"type": "message_end", "message": failure})
            await events.emit({"type": "turn_end", "message": failure, "tool_results": []})
            await events.emit({"type": "agent_end", "messages": messages[start:]})
            raise
        messages.append(response)
        tool_results: list[ToolResultMessage] = []
        tool_started = False
        try:
            # s04 新增：等待回答的 message_end 订阅器，再进入工具校验和执行。
            await events.emit({"type": "message_start", "message": response})
            await events.emit({"type": "message_end", "message": response})
            if response.stop_reason not in {"error", "aborted"}:
                for call in calls:
                    tool_started = True
                    await events.emit(
                        {
                            "type": "tool_execution_start",
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "args": call.arguments,
                        }
                    )
                    # 来自 s01：保持；截断请求整批拒绝，普通失败由 execute_tool 回传。
                    if response.stop_reason == "length":
                        result = ToolResultMessage(
                            call.id, call.name, "工具调用被截断，请重新完整调用", True
                        )
                    else:
                        result = await execute_tool(call, catalog)
                    # s04 新增：先保存真实结果，再通知订阅器，避免显示失败丢失已执行结果。
                    messages.append(result)
                    tool_results.append(result)
                    tool_started = False
                    await events.emit(
                        {
                            "type": "tool_execution_end",
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "result": ToolResult(result.content, result.is_error),
                            "is_error": result.is_error,
                        }
                    )
                    await events.emit({"type": "message_start", "message": result})
                    await events.emit({"type": "message_end", "message": result})
        except (Exception, asyncio.CancelledError) as error:
            # s04 修改：新增的等待点也可能被取消/观察者中断；先同步补齐全部未完成调用。
            pending = (
                calls[len(tool_results) :]
                if response.stop_reason not in {"error", "aborted"}
                else []
            )
            reason = (
                "工具执行已取消"
                if isinstance(error, asyncio.CancelledError)
                else "运行已中断，工具未完成"
            )
            unfinished = [ToolResultMessage(call.id, call.name, reason, True) for call in pending]
            messages.extend(unfinished)
            tool_results.extend(unfinished)
            if isinstance(error, asyncio.CancelledError):
                # s04 新增：单次原生取消时完成事件通知；二次取消或订阅器失败仍直接传播。
                for index, (call, result) in enumerate(zip(pending, unfinished, strict=True)):
                    if index or not tool_started:
                        await events.emit(
                            {
                                "type": "tool_execution_start",
                                "tool_call_id": call.id,
                                "tool_name": call.name,
                                "args": call.arguments,
                            }
                        )
                    await events.emit(
                        {
                            "type": "tool_execution_end",
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "result": ToolResult(result.content, True),
                            "is_error": True,
                        }
                    )
                    await events.emit({"type": "message_start", "message": result})
                    await events.emit({"type": "message_end", "message": result})
                await events.emit(
                    {"type": "turn_end", "message": response, "tool_results": tool_results}
                )
                await events.emit({"type": "agent_end", "messages": messages[start:]})
            raise
        # s04 新增：回合包括一次回答和整批工具结果；末事件的订阅器也必须等待。
        await events.emit({"type": "turn_end", "message": response, "tool_results": tool_results})
        if response.stop_reason in {"error", "aborted"} or not calls:
            await events.emit({"type": "agent_end", "messages": messages[start:]})
            return messages
    await events.emit({"type": "agent_end", "messages": messages[start:]})
    # 来自 s01：保持；显式轮数保护结束后，结果保留但通过异常报告未完成。
    raise TurnLimitError(f"已达到 {max_turns} 轮上限；任务尚未完成")
