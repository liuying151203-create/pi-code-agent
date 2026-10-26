"""来自 s15/s21：唯一产品核心循环；业务流程通过工具和回调注入。"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from pi_code_agent import serialization as s05
from pi_code_agent.events import AgentEvent, EventDispatcher, EventDispatchError, EventSink
from pi_code_agent.hooks import Hooks
from pi_code_agent.observable_session import AssistantSaver
from pi_code_agent.permissions import TOOLS
from pi_code_agent.requests import _tool_result_is_error
from pi_code_agent.result import DispatchTool, ToolResult, execute_tool
from pi_code_agent.sandbox_copy.sandbox import SandboxError
from pi_code_agent.serialization import Message, SessionSaver, _get
from pi_code_agent.session import SessionPersistenceError, ToolDetails, ToolResultSaver


def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., Any],
    dispatch: DispatchTool,
    system: str,
    # s01 修改：基础 SDK 调用无需组装后续章节的 Hook 和观察者，显式传入时保持原行为。
    hooks: Hooks | None = None,
    emit: EventSink | None = None,
    save_message: SessionSaver | None = None,
    save_assistant: AssistantSaver | None = None,
    # 来自 s15：保持；单独注入工具批次保存回调，正文和 details 同时落盘。
    save_tool_results: ToolResultSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
    # 来自 s12：保持；限轮仅在调用方明确配置时启用，不是默认完成策略。
    max_rounds: int | None = None,
) -> list[Message]:
    """运行带工具调用、会话保存和事件观察的 Agent 循环。

    输入：模型消息、请求器、工具分发器、system、Hooks 和保存回调。
    输出：本次运行的完整消息；没有工具调用或模型返回失败状态时结束，超限时报错。
    流程：请求模型 → 保存 assistant → 执行工具 → 保存正文与状态 → 发送结果事件 → 再次请求。
    带 details 的结果必须有专用保存回调；保存失败不发送成功事件，后端失败仍补齐配对。
    """
    # s01 新增：正式 SDK 与最小教程使用同一调用契约，不另建产品核心循环。
    hooks = hooks if hooks is not None else Hooks()
    emit = emit if emit is not None else EventDispatcher().emit
    # 来自 s12：保持；轮数只计算回答请求；摘要由请求包装器负责，不隐含在计数中。
    # 来自 s12：保持；默认不限制轮数，有明确配置时才停止。
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max_rounds 必须为正整数")
    # 来自 s12：保持；只有 None 回退默认工具，显式空列表不扩大能力。
    active_tools = TOOLS if tools is None else tools
    emit(AgentEvent(type="agent_start", data={"message_count": len(messages)}))

    # 来自 s12：保持；显式统计回答请求，None 时不设置硬停止点。
    round_count = 0
    while max_rounds is None or round_count < max_rounds:
        round_count += 1
        # 来自 s12：保持；摘要或回答请求失败同样结束本次任务，观察者不会收到悬空生命周期。
        try:
            response = create_message(
                system=system,
                messages=messages,
                tools=active_tools,
                max_tokens=max_tokens,
            )
        except EventDispatchError:
            # 来自 s11：保持；观察链失败后不再次广播结束事件。
            raise
        except (RuntimeError, ValueError):
            emit(
                AgentEvent(
                    type="agent_end",
                    data={"messages": list(messages), "reason": "request_failed"},
                )
            )
            raise
        assistant_message = {
            "role": "assistant",
            "content": s05._to_jsonable(response.content),
        }
        messages.append(assistant_message)
        if save_assistant is not None:
            save_assistant(assistant_message, response)
        elif save_message is not None:
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        if response.stop_reason in {"error", "aborted"}:
            # s21 修改：已保存失败回答与用量后停止任务，不能接着验收/标记通过。
            emit(
                AgentEvent(
                    type="agent_end", data={"messages": list(messages), "reason": "model_failed"}
                )
            )
            raise RuntimeError("模型返回失败或取消，已停止任务，不进入测试验收")

        tool_calls = [
            block for block in assistant_message["content"] if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        # 来自 s15：保持；元数据按调用 ID 保存，事件暂存到批次成功落盘之后。
        tool_details: ToolDetails = {}
        result_events: list[AgentEvent] = []
        # 来自 s14：保持；致命后端失败后补齐剩余工具结果，但不继续执行。
        backend_failure: str | None = None
        for block in tool_calls:
            tool_call_id = str(_get(block, "id") or "")
            name = str(_get(block, "name") or "")
            arguments = _get(block, "input")
            if arguments is None:
                arguments = {}
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"tool_call_id": tool_call_id, "name": name, "arguments": arguments},
                )
            )

            tool_started_at = perf_counter()
            if backend_failure is not None:
                # 来自 s15：保持；错误也使用统一结果对象，但没有成功状态快照。
                result = ToolResult("Error: tool execution stopped; this tool was not executed")
            # s01 修改：按 Pi prepareToolCall 的顺序先校验声明及参数，不执行未知工具或截断调用。
            elif _get(response, "stop_reason") in {"max_tokens", "length"}:
                result = ToolResult("Error: tool call was truncated; retry with a complete call")
            elif name not in {tool["name"] for tool in active_tools}:
                result = ToolResult(f"Error: unknown tool: {name}")
            elif not isinstance(arguments, dict):
                result = ToolResult("Error: tool arguments must be an object")
            else:
                try:
                    # 来自 s15：保持；已有 Hook 仍处理正文，结构化结果另外携带状态。
                    result = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
                except (SandboxError, KeyboardInterrupt) as error:
                    backend_failure = str(error) or "用户中断工具执行"
                    result = ToolResult(f"Error: {backend_failure}")
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000

            # 来自 s15：保持；模型消息保持标准 tool_result 字段，details 不进入模型协议。
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": result.content,
                    # s01 修改：错误状态属于工具协议，不能只在界面事件里保存。
                    "is_error": _tool_result_is_error(result.content),
                }
            )
            if result.details is not None:
                # 来自 s15：保持；名称和调用 ID 来自本次调用，不从模型结果正文猜测。
                tool_details[tool_call_id] = {"name": name, "details": result.details}
            # 来自 s15：保持；沿用已有事件名，结果事件稍后发送；每次调用仍只计数一次。
            result_events.append(
                AgentEvent(
                    type="tool_result",
                    data={
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "output": result.content,
                        "details": result.details,
                        "is_error": _tool_result_is_error(result.content),
                        "duration_ms": tool_duration_ms,
                    },
                )
            )

        tool_message = {"role": "user", "content": results}
        # 来自 s15：保持；先保存完整批次，再更新内存上下文和通知观察者，避免显示未保存进度。
        try:
            if save_tool_results is not None:
                save_tool_results(tool_message, tool_details)
            elif tool_details:
                raise SessionPersistenceError(
                    "带 details 的工具结果必须提供 save_tool_results，不能丢弃状态"
                )
            elif save_message is not None:
                save_message(tool_message)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            emit(
                AgentEvent(
                    type="agent_end",
                    data={"messages": list(messages), "reason": "persistence_failed"},
                )
            )
            raise SessionPersistenceError(f"工具结果保存失败，已停止运行：{error}") from error
        messages.append(tool_message)
        # 来自 s15：保持；使用各次结果的独立快照，不能把同批较早事件都显示为最终状态。
        for event in result_events:
            emit(event)
        # 来自 s13-A：保持；先保存整个批次，再终止；加载历史时不会留下缺失 tool_result。
        if backend_failure is not None:
            emit(
                AgentEvent(
                    # 来自 s14：保持；通用分发器的失败事件不声称本次使用了沙箱。
                    type="agent_end",
                    data={"messages": list(messages), "reason": "tool_backend_failed"},
                )
            )
            raise SandboxError(backend_failure)

    # 来自 s12：保持；保留已完成工具结果并报告未完成，不伪造一次模型最终回答。
    emit(
        AgentEvent(
            type="agent_end",
            data={"messages": list(messages), "reason": "max_rounds"},
        )
    )
    raise RuntimeError(f"已达到 {max_rounds} 轮模型请求上限，任务尚未完成；已停止自动调用工具。")
