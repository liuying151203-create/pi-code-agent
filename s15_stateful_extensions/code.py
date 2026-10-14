#!/usr/bin/env python3
"""s15：能力系统 · 有状态扩展（Todo）。

扩展通过 ToolResult.details 保存状态，循环先保存工具批次，再发出结果事件。
Todo 只是一个受信任扩展，循环和会话层不包含任务清单专用分支。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

# 来自 s12：保持；复用已有基础工具、模型、会话与上下文组件。
from s03_permission import code as permissions
from s05_session_persistence import code as s05
from s11_runtime_observability import code as runtime

# 来自 s13-A：保持；已有致命后端错误仍要终止，并补齐当前工具批次结果。
from s13_a_sandbox_copy.sandbox import SandboxError
from s14_extensions import code as previous

# s15 修改：注册入口增加会话查询与结构化结果，仍在 main 组装，不进入循环业务。
from s15_stateful_extensions.registry import ExtensionAPI, load_extensions

# s15 新增：结果正文和状态分开保存，循环仍不理解具体扩展的状态结构。
from s15_stateful_extensions.result import DispatchTool, ToolResult, execute_tool
from s15_stateful_extensions.session import (
    SessionManager,
    SessionPersistenceError,
    ToolDetails,
    ToolResultSaver,
)
from zero2pi.ui import format_error, format_user_prompt

# ===== 来自 s14：运行时、工具与技能（保持） =====

Message = previous.Message
ModelResponse = previous.ModelResponse
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
AssistantSaver = previous.AssistantSaver
EventSink = previous.EventSink
AgentEvent = previous.AgentEvent
CompactionPolicy = previous.CompactionPolicy
ContextCompactor = previous.ContextCompactor
CompactedContextRequester = previous.CompactedContextRequester
BlockingModelRequester = previous.BlockingModelRequester
StreamingModelRequester = previous.StreamingModelRequester
EventDispatcher = previous.EventDispatcher
# 来自 s11：保持；扩展观察者故障停止入口，不继续请求模型。
EventDispatchError = runtime.EventDispatchError
TerminalEventSink = previous.TerminalEventSink
JsonlTraceRecorder = previous.JsonlTraceRecorder
UsageTracker = previous.UsageTracker
SessionErrorRecorder = previous.SessionErrorRecorder
SYSTEM = previous.SYSTEM
make_permission_hook = previous.make_permission_hook
create_model_provider = previous.create_model_provider
summary_request_options = previous.summary_request_options
_positive_int_env = previous._positive_int_env
_get = previous._get
_tool_result_is_error = previous._tool_result_is_error
ContextSummarizer = previous.ContextSummarizer
show_summary_diagnostic = previous.show_summary_diagnostic
model_round_limit_from_env = previous.model_round_limit_from_env
SkillCatalog = previous.SkillCatalog
build_system_prompt = previous.build_system_prompt
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
SKILLS_ROOT = previous.SKILLS_ROOT

# s15 修改：会话和 Trace 按本章隔离，工具状态不写入 s14 的历史。
SESSION_ROOT = Path(".sessions/s15")
TRACE_ROOT = Path(".traces/s15")
# s15 修改：默认加载 Todo 模块，自带 read_preview 仍可显式加载，不扫描项目脚本。
DEFAULT_EXTENSION = "s15_stateful_extensions.extensions.todo"


# ===== 来自 s14：启动选项 =====


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """解析本章参数，不导入扩展或创建运行文件。

    输入：--mode、--session 及可重复的 --extension Python 模块名。
    输出：Namespace；未指定扩展时选择 Harness 自带示例，显式指定则替换默认。
    流程：argparse 校验 → 返回选项；--help 在注册、模型初始化之前退出。
    """
    # 来自 s14：保持；扩展属于启动配置，不作为模型工具或对话命令加载。
    parser = argparse.ArgumentParser(description="s15：有状态 Todo 扩展")
    parser.add_argument("--mode", choices=("default", "read-only"), default="default")
    parser.add_argument("--session", type=Path, help="恢复本章 JSONL 会话")
    parser.add_argument(
        "--extension", action="append", metavar="MODULE", help="显式加载受信任模块；可重复"
    )
    return parser.parse_args(arguments)


# ===== s15 修改：完整核心循环接入结果元数据 =====
# 来自 s14：保持；循环不知道注册模块和 Todo 业务，只处理通用工具结果。


def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., Any],
    dispatch: DispatchTool,
    system: str,
    hooks: Hooks,
    emit: EventSink,
    save_message: SessionSaver | None = None,
    save_assistant: AssistantSaver | None = None,
    # s15 新增：单独注入工具批次保存回调，正文和 details 同时落盘。
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
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        tool_calls = [
            block for block in assistant_message["content"] if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        # s15 新增：元数据按调用 ID 保存，事件暂存到批次成功落盘之后。
        tool_details: ToolDetails = {}
        result_events: list[AgentEvent] = []
        # 来自 s14：保持；致命后端失败后补齐剩余工具结果，但不继续执行。
        backend_failure: str | None = None
        for block in tool_calls:
            tool_call_id = str(_get(block, "id") or "")
            name = str(_get(block, "name") or "")
            arguments = _get(block, "input") or {}
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"tool_call_id": tool_call_id, "name": name, "arguments": arguments},
                )
            )

            tool_started_at = perf_counter()
            if backend_failure is not None:
                # s15 修改：错误也使用统一结果对象，但没有成功状态快照。
                result = ToolResult("Error: tool execution stopped; this tool was not executed")
            else:
                try:
                    # s15 修改：已有 Hook 仍处理正文，结构化结果另外携带状态。
                    result = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
                except (SandboxError, KeyboardInterrupt) as error:
                    backend_failure = str(error) or "用户中断工具执行"
                    result = ToolResult(f"Error: {backend_failure}")
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000

            # s15 修改：模型消息保持标准 tool_result 字段，details 不进入模型协议。
            results.append(
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": result.content}
            )
            if result.details is not None:
                # s15 新增：名称和调用 ID 来自本次调用，不从模型结果正文猜测。
                tool_details[tool_call_id] = {"name": name, "details": result.details}
            # s15 修改：沿用已有事件名，结果事件稍后发送；每次调用仍只计数一次。
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
        # s15 修改：先保存完整批次，再更新内存上下文和通知观察者，避免显示未保存进度。
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
        # s15 新增：使用各次结果的独立快照，不能把同批较早事件都显示为最终状态。
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


# ===== s15 修改：入口显式组装扩展注册层 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动支持状态恢复和进度提示的扩展 Agent。

    输入：模型配置、受信任扩展模块、启动模式、可选历史会话和用户任务。
    输出：回答、扩展观察提示，以及 s15 独立会话、Trace 和用量统计。
    流程：基础工具与会话 → 加载扩展并恢复状态 → 功能组件 → 运行时组装 → 交互。
    边界：扩展在宿主执行，只能加载可信模块；本章不默认启动 s13 的沙箱后端。
    """
    # ===== 来自 s12：基础工具与权限（保持） =====
    options = parse_args(arguments)
    base_tools = permissions.READ_ONLY_TOOLS if options.mode == "read-only" else TOOLS
    permission = (
        permissions.check_read_only_permission
        if options.mode == "read-only"
        else permissions.check_permission
    )
    permission_hook = make_permission_hook(permission=permission)

    # s15 修改：先打开会话，register 才能从完整日志恢复本扩展的状态。
    session_path = s05.select_session_path(options.session, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)

    # ===== s15 修改：扩展组件接入会话恢复 =====
    # 来自 s14：保持；权限交给注册层的基础委托入口，确保包装工具也检查真实操作。
    extensions = ExtensionAPI(
        base_tools=base_tools,
        base_dispatch=dispatch_tool,
        permission_hook=permission_hook,
        # s15 新增：状态读取是本章明确注入的依赖，不让扩展查找全局会话。
        session=session,
    )
    # s15 修改：已打开会话后加载扩展，模型组件仍在注册成功后创建。
    extension_modules = options.extension if options.extension is not None else [DEFAULT_EXTENSION]
    loaded_extensions = load_extensions(extensions, extension_modules)

    # ===== 来自 s12：模型、会话与技能（保持） =====
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
    provider = create_model_provider(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    skill_catalog = SkillCatalog.scan(SKILLS_ROOT)
    system_prompt = build_system_prompt(SYSTEM, skill_catalog)

    # ===== 来自 s12：观察层与模型请求（保持） =====
    terminal = TerminalEventSink()
    trace_path = runtime.new_trace_path(TRACE_ROOT)
    trace_recorder = JsonlTraceRecorder(trace_path)
    usage_tracker = UsageTracker()
    error_recorder = SessionErrorRecorder(session)
    events = EventDispatcher()
    events.subscribe(terminal)
    events.subscribe(trace_recorder)
    events.subscribe(usage_tracker)
    events.subscribe(error_recorder)
    events.subscribe(show_summary_diagnostic)
    # 来自 s14：保持；扩展仍订阅同一总线，循环在保存结果后才发送含快照的事件。
    events.subscribe(extensions.handle_event)
    summary_requester = BlockingModelRequester(provider, timeout_seconds, events.emit)
    assistant_requester = StreamingModelRequester(provider, timeout_seconds, events.emit)

    # ===== 来自 s12：上下文与运行时组装（保持） =====
    policy = CompactionPolicy(
        context_window_tokens=_positive_int_env("MODEL_CONTEXT_WINDOW_TOKENS", 0),
        reserve_tokens=_positive_int_env("SESSION_COMPACTION_RESERVE_TOKENS", 16384),
        keep_recent_tokens=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000),
    )
    summary_default_tokens = int(policy.reserve_tokens * 0.8)
    summary_max_tokens = _positive_int_env(
        "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", summary_default_tokens
    )
    summary_options = summary_request_options(provider)
    summarizer = ContextSummarizer(
        create_message=summary_requester,
        max_tokens=summary_max_tokens,
        session=session,
        emit=events.emit,
        request_options=summary_options,
    )
    compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
    request_with_context = CompactedContextRequester(
        create_message=assistant_requester,
        session=session,
        compactor=compactor,
        emit=events.emit,
    )
    # 来自 s14：保持；目录和 Hooks 来自扩展注册结果，基础权限仍在 extensions.call_tool 检查。
    active_tools = extensions.tools
    hooks = extensions.hooks
    round_limit = model_round_limit_from_env()

    # ===== s15 修改：交互入口展示注册结果 =====
    print("s15：能力系统 · 有状态扩展（Todo）")
    print(f"模型：{provider.model}；工具模式：{options.mode}")
    print(f"扩展：{', '.join(loaded_extensions)}")
    print(f"可用工具：{', '.join(tool['name'] for tool in active_tools)}")
    print(f"会话：{session.path}；Trace：{trace_path}")
    print("输入任务，输入 q 退出。扩展是受信任的宿主 Python，不是沙箱。\n")

    # ===== 来自 s12：任务、保存与统计（保持） =====
    while True:
        try:
            # 来自 s14：保持；提示符标明当前章节，便于区分各章入口。
            query = input(format_user_prompt("s15")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        session.append_message(user_message)
        active_context = session.build_context()
        try:
            agent_loop(
                active_context,
                create_message=request_with_context,
                # 来自 s14：保持；一次分发包含内置和扩展工具，不在循环中判断来源。
                dispatch=extensions.dispatch,
                system=system_prompt,
                hooks=hooks,
                tools=active_tools,
                emit=events.emit,
                save_message=session.append_message,
                save_assistant=session.append_assistant,
                # s15 新增：专用保存入口同时写正文和状态，不把任务逻辑塞进循环。
                save_tool_results=session.append_tool_results,
                max_rounds=round_limit,
            )
        except (SandboxError, SessionPersistenceError, EventDispatchError):
            # s15 修改：后端、保存或观察层故障都退出，已保存状态仍可恢复，不自动重放工具。
            raise
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
        finally:
            print(usage_tracker.summary_text())
            print(session.session_stats().summary_text())
            print()


if __name__ == "__main__":
    try:
        main()
    # 来自 s14：保持；注册和参数错误在启动阶段清晰报告，不假装加载成功。
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
