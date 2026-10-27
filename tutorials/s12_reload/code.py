"""s05：保留顺序工具循环，流式更新同一回答，完成后才执行工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import aclosing

from .errors import TurnLimitError
from .events import EventBus, EventListenerError
from .messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    convert_to_llm,
)
from .model import Model, StreamingModel
from .tools import Tool, ToolResult, execute_tool_outcome


# ===== s05 修改 =====
# 来自 s05：保持；保留 s04 的事件边界，流式更新同一回答，等最终数据到齐再执行工具。
async def agent_loop(
    messages: list[Message],
    *,
    model: Model | StreamingModel,
    tools: Sequence[Tool] = (),
    system: str = "你是编程助手，完成任务后停止。",
    max_tokens: int = 8000,
    max_turns: int | None = None,
    prompts: Sequence[Message] = (),
    events: EventBus | None = None,
    # 来自 s05：保持；保留完整响应路径，调用方明确选择流式，不猜测模型能力。
    stream: bool = False,
    # 来自 s08：保持；核心只接收可调用策略，不依赖扩展加载器。
    transform_context=None,
    before_tool_call=None,
    after_tool_call=None,
    # 来自 s10：保持；只在完整回合边界读取消息队列。
    get_steering_messages=None,
    get_follow_up_messages=None,
    after_turn=None,
    # s12 新增：请求前固定当前工具快照，批次中不替换分发目录。
    get_tools=None,
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
    if type(stream) is not bool:
        raise ValueError("stream 必须是布尔值")
    if stream and not callable(getattr(model, "stream", None)):
        raise ValueError("模型没有 stream()；请使用完整响应模式")
    catalog = {tool.name: tool for tool in tools}
    if len(catalog) != len(tools):
        raise ValueError("本次工具目录存在重名")
    definitions = [tool.definition() for tool in tools]
    # 来自 s04：保持；对应 Pi prompts/context 分离；agent_end 只携带本次新增消息。
    events = events if events is not None else EventBus()
    start = len(messages)
    initial = list(prompts)
    messages.extend(initial)
    await events.emit({"type": "agent_start"})
    await events.emit({"type": "turn_start"})
    for message in initial:
        await events.emit({"type": "message_start", "message": message})
        await events.emit({"type": "message_end", "message": message})
    pending = await get_steering_messages() if get_steering_messages else []
    turn = 0
    while max_turns is None or turn < max_turns:
        if turn:
            await events.emit({"type": "turn_start"})
        turn += 1
        # 来自 s10：保持；一轮只消费一次队列，保持 one-at-a-time 语义。
        for queued in pending:
            messages.append(queued)
            await events.emit({"type": "message_start", "message": queued})
            await events.emit({"type": "message_end", "message": queued})
        pending = []
        # 来自 s05：保持；流中的回答只占一个历史位置，每次更新替换快照，完成后才进入工具阶段。
        message_index = None
        partial = AssistantMessage((), "pending")
        response = None
        try:
            if get_tools is not None:
                current_tools = tuple(await get_tools())
                catalog = {tool.name: tool for tool in current_tools}
                if len(catalog) != len(current_tools):
                    raise ValueError("动态工具目录存在重名")
                definitions = [tool.definition() for tool in current_tools]
            request_messages = await transform_context(messages) if transform_context else messages
            request_messages = convert_to_llm(request_messages)
            if stream:
                # 来自 s05：保持；即使订阅器失败或任务取消，也先关闭模型生成器及底层 HTTP 流。
                async with aclosing(
                    model.stream(
                        system=system,
                        messages=tuple(request_messages),
                        tools=definitions,
                        max_tokens=max_tokens,
                    )
                ) as chunks:
                    async for event in chunks:
                        if response is not None:
                            raise ValueError("模型流在 done 之后仍返回事件")
                        if event["type"] == "start":
                            if message_index is not None:
                                raise ValueError("模型流重复 start")
                            snapshot = event["partial"]
                            if (
                                not isinstance(snapshot, AssistantMessage)
                                or snapshot.stop_reason != "pending"
                            ):
                                raise ValueError("模型流 start 必须携带 pending 快照")
                            partial = snapshot
                            message_index = len(messages)
                            messages.append(partial)
                            await events.emit({"type": "message_start", "message": partial})
                        elif event["type"] in {"text_start", "text_delta", "text_end"}:
                            if message_index is None:
                                raise ValueError("模型流文本事件早于 start")
                            snapshot = event["partial"]
                            if (
                                not isinstance(snapshot, AssistantMessage)
                                or snapshot.stop_reason != "pending"
                            ):
                                raise ValueError("模型流文本事件必须携带 pending 快照")
                            partial = snapshot
                            messages[message_index] = partial
                            await events.emit(
                                {
                                    "type": "message_update",
                                    "message": partial,
                                    "assistant_message_event": event,
                                }
                            )
                        elif event["type"] == "done":
                            if message_index is None:
                                raise ValueError("模型流 done 早于 start")
                            response = event["message"]
                        else:
                            raise ValueError(f"当前不支持模型流事件：{event['type']}")
                if response is None:
                    raise ValueError("模型流没有最终 done，回答未完成")
            else:
                # 来自 s04：保持；旧 Model 和完整响应调用仍然可用。
                response = await model.complete(
                    system=system,
                    messages=tuple(request_messages),
                    tools=definitions,
                    max_tokens=max_tokens,
                )
            if not isinstance(response, AssistantMessage):
                raise TypeError("模型必须返回 AssistantMessage")
            if response.stop_reason == "pending":
                raise ValueError("pending 快照不能作为最终回答")
            calls = [block for block in response.content if isinstance(block, ToolCall)]
            if response.stop_reason not in {"error", "aborted"} and len(
                {call.id for call in calls}
            ) != len(calls):
                raise ValueError("模型返回重复的工具调用 ID，已停止执行")
        except EventListenerError as error:
            # 来自 s05：保持；观察失败单独传播，不伪造供应商失败，也不向坏订阅器递归通知。
            if message_index is not None:
                messages[message_index] = AssistantMessage(
                    partial.content, "error", partial.usage, f"事件观察失败：{error}"
                )
            raise
        except (Exception, asyncio.CancelledError) as error:
            # 来自 s05：保持；中断时保留已经收到的实际文本和用量，不追加第二条模型回答。
            # 来自 s05：保持；Provider 还可能收到未触发文本事件的元数据，用最新快照完成失败记录。
            received = getattr(error, "partial", None)
            if isinstance(received, AssistantMessage):
                partial = received
            reason = "aborted" if isinstance(error, asyncio.CancelledError) else "error"
            failure = AssistantMessage(
                partial.content, reason, partial.usage, str(error) or "运行已取消"
            )
            if message_index is None:
                messages.append(failure)
                await events.emit({"type": "message_start", "message": failure})
            else:
                messages[message_index] = failure
            await events.emit({"type": "message_end", "message": failure})
            await events.emit({"type": "turn_end", "message": failure, "tool_results": []})
            await events.emit({"type": "agent_end", "messages": messages[start:]})
            raise
        if message_index is None:
            messages.append(response)
        else:
            messages[message_index] = response
        tool_results: list[ToolResultMessage] = []
        tool_started = False
        terminated = []
        try:
            # 来自 s04：保持；等待回答的 message_end 订阅器，再进入工具校验和执行。
            # 来自 s05：保持；流式 start 已经通知过，只为完整响应补发一次开始事件。
            if message_index is None:
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
                        outcome = await execute_tool_outcome(
                            call, catalog, before=before_tool_call, after=after_tool_call
                        )
                        result = ToolResultMessage(
                            call.id, call.name, outcome.content, outcome.is_error
                        )
                        terminated.append(outcome.terminate)
                    # 来自 s04：保持；先保存真实结果，再通知订阅器，避免显示失败丢失已执行结果。
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
            # 来自 s04：保持；新增的等待点也可能被取消/观察者中断；先同步补齐全部未完成调用。
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
                # 来自 s04：保持；单次原生取消时完成事件通知；二次取消或订阅器失败仍直接传播。
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
        # 来自 s04：保持；回合包括一次回答和整批工具结果；末事件的订阅器也必须等待。
        await events.emit({"type": "turn_end", "message": response, "tool_results": tool_results})
        # s10 修复：等待所有 turn_end 观察者后再投递，避免末回合新消息滞留。
        if after_turn is not None:
            await after_turn()
        if response.stop_reason in {"error", "aborted"}:
            await events.emit({"type": "agent_end", "messages": messages[start:]})
            return messages
        pending = await get_steering_messages() if get_steering_messages else []
        needs_tools = bool(calls) and not (len(terminated) == len(calls) and all(terminated))
        if not needs_tools and not pending:
            pending = await get_follow_up_messages() if get_follow_up_messages else []
            if not pending:
                await events.emit({"type": "agent_end", "messages": messages[start:]})
                return messages
    # s10 修复：达到轮数保护时，已经出队的输入仍保存到历史，后续 continue_ 不丢消息。
    for queued in pending:
        messages.append(queued)
        await events.emit({"type": "message_start", "message": queued})
        await events.emit({"type": "message_end", "message": queued})
    await events.emit({"type": "agent_end", "messages": messages[start:]})
    # 来自 s01：保持；显式轮数保护结束后，结果保留但通过异常报告未完成。
    raise TurnLimitError(f"已达到 {max_turns} 轮上限；任务尚未完成")


# s12 新增：本章入口明确注入本章完整循环，不引用正式包或其他章节。
def main(arguments=None):
    """组装本章组件，输入命令行参数，输出退出码。"""
    from .cli import main as cli_main

    return cli_main(arguments, loop=agent_loop)


if __name__ == "__main__":
    raise SystemExit(main())
