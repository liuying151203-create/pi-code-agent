#!/usr/bin/env python3
"""s14：能力系统 · Extensions。

受信任扩展在启动时注册工具、Hook 和事件观察者；核心循环仍只接收工具目录和分发函数。
基础工具权限、技能、会话、压缩、流式响应及统计沿用 s12；本入口不启用 Docker 或原生沙箱。
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
from s12_skill_loading import code as previous

# 来自 s13-A：保持；已有致命后端错误仍要终止，并补齐当前工具批次结果。
from s13_a_sandbox_copy.sandbox import SandboxError

# s14 新增：注册机制是独立组件，组装在 main，不进入核心循环。
from s14_extensions.registry import ExtensionAPI, load_extensions
from zero2pi.ui import format_error, format_user_prompt

# ===== 来自 s12：运行时、工具与技能（保持） =====

Message = previous.Message
ModelResponse = previous.ModelResponse
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
AssistantSaver = previous.AssistantSaver
EventSink = previous.EventSink
AgentEvent = previous.AgentEvent
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextCompactor = previous.ContextCompactor
CompactedContextRequester = previous.CompactedContextRequester
BlockingModelRequester = previous.BlockingModelRequester
StreamingModelRequester = previous.StreamingModelRequester
EventDispatcher = previous.EventDispatcher
TerminalEventSink = previous.TerminalEventSink
JsonlTraceRecorder = previous.JsonlTraceRecorder
UsageTracker = previous.UsageTracker
SessionErrorRecorder = previous.SessionErrorRecorder
SYSTEM = previous.SYSTEM
execute_tool = previous.execute_tool
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

# s14 修改：本章会话和 Trace 单独存放，不与 s12 的演示数据混用。
SESSION_ROOT = Path(".sessions/s14")
TRACE_ROOT = Path(".traces/s14")
# s14 新增：默认只加载 Harness 自带示例，不自动执行目标仓库中的 Python。
DEFAULT_EXTENSION = "s14_extensions.extensions.read_preview"


# ===== s14 新增：启动选项 =====


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """解析本章参数，不导入扩展或创建运行文件。

    输入：--mode、--session 及可重复的 --extension Python 模块名。
    输出：Namespace；未指定扩展时选择 Harness 自带示例，显式指定则替换默认。
    流程：argparse 校验 → 返回选项；--help 在注册、模型初始化之前退出。
    """
    # s14 新增：扩展属于启动配置，不作为模型工具或对话命令加载。
    parser = argparse.ArgumentParser(description="s14：受信任 Python Extensions")
    parser.add_argument("--mode", choices=("default", "read-only"), default="default")
    parser.add_argument("--session", type=Path, help="恢复本章 JSONL 会话")
    parser.add_argument(
        "--extension", action="append", metavar="MODULE", help="显式加载受信任模块；可重复"
    )
    return parser.parse_args(arguments)


# ===== 来自 s13-A：完整核心循环（保持） =====
# s14 不在循环中增加注册分支；本入口使用宿主工具，但保留后端致命失败的批次配对保护。


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
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
    # 来自 s12：保持；限轮仅在调用方明确配置时启用，不是默认完成策略。
    max_rounds: int | None = None,
) -> list[Message]:
    """运行带工具调用、会话保存和事件观察的 Agent 循环。

    输入：模型消息、请求器、工具分发器、system、Hooks 和保存回调。
    输出：本次运行的完整消息；没有工具调用或模型返回失败状态时结束，超限时报错。
    流程：请求模型 → 保存 assistant 与 usage → 执行工具 → 保存结果 → 再次请求。
    扩展结果与普通结果同样保存；后端致命失败时补齐工具结果批次，然后停止。
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
        # 来自 s13-A：保持；致命后端失败后不再执行剩余工具，但必须补齐结果配对。
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
            # 来自 s13-A：保持；只走注入的分发入口，致命失败不继续执行或切换后端。
            if backend_failure is not None:
                # s14 修改：本入口并非容器，停止提示使用通用工具执行名称。
                output = "Error: tool execution stopped; this tool was not executed"
            else:
                try:
                    output = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
                except (SandboxError, KeyboardInterrupt) as error:
                    # s14 修改：中断提示不限定隔离环境，仍停止当前工具批次。
                    backend_failure = str(error) or "用户中断工具执行"
                    output = f"Error: {backend_failure}"
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000
            emit(
                AgentEvent(
                    type="tool_result",
                    data={
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "output": output,
                        "is_error": _tool_result_is_error(output),
                        "duration_ms": tool_duration_ms,
                    },
                )
            )
            results.append({"type": "tool_result", "tool_use_id": tool_call_id, "content": output})

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)
        # 来自 s13-A：保持；先保存整个批次，再终止；加载历史时不会留下缺失 tool_result。
        if backend_failure is not None:
            emit(
                AgentEvent(
                    # s14 修改：通用分发器的失败事件不声称本次使用了沙箱。
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


# ===== s14 修改：入口显式组装扩展注册层 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动支持工具、Hook 和事件注册的 Agent。

    输入：模型配置、受信任扩展模块、启动模式、可选历史会话和用户任务。
    输出：回答、扩展观察提示，以及 s14 独立会话、Trace 和用量统计。
    流程：基础工具与权限 → 加载扩展 → 模型与功能组件 → 运行时组装 → 交互。
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

    # ===== s14 新增：扩展组件 =====
    # s14 新增：权限交给注册层的基础委托入口，确保包装工具也检查真实操作。
    extensions = ExtensionAPI(
        base_tools=base_tools,
        base_dispatch=dispatch_tool,
        permission_hook=permission_hook,
    )
    # s14 新增：先完成注册，再创建会话或模型组件；失败时不进入半注册的运行时。
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
    session_path = s05.select_session_path(options.session, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)
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
    # s14 新增：注册层只筛选已订阅的事件，仍复用 s11 的同一事件总线。
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
    # s14 修改：目录和 Hooks 来自扩展注册结果，基础权限仍在 extensions.call_tool 检查。
    active_tools = extensions.tools
    hooks = extensions.hooks
    round_limit = model_round_limit_from_env()

    # ===== s14 修改：交互入口展示注册结果 =====
    print("s14：能力系统 · Extensions")
    print(f"模型：{provider.model}；工具模式：{options.mode}")
    print(f"扩展：{', '.join(loaded_extensions)}")
    print(f"可用工具：{', '.join(tool['name'] for tool in active_tools)}")
    print(f"会话：{session.path}；Trace：{trace_path}")
    print("输入任务，输入 q 退出。扩展是受信任的宿主 Python，不是沙箱。\n")

    # ===== 来自 s12：任务、保存与统计（保持） =====
    while True:
        try:
            # s14 修改：提示符标明当前章节，便于区分各章入口。
            query = input(format_user_prompt("s14")).strip()
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
                # s14 修改：一次分发包含内置和扩展工具，不在循环中判断来源。
                dispatch=extensions.dispatch,
                system=system_prompt,
                hooks=hooks,
                tools=active_tools,
                emit=events.emit,
                save_message=session.append_message,
                save_assistant=session.append_assistant,
                max_rounds=round_limit,
            )
        except SandboxError:
            # 来自 s13-A：保持；后端致命失败必须退出，不开始下一任务或换到宿主执行。
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
    # s14 新增：注册和参数错误在启动阶段清晰报告，不假装加载成功。
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
