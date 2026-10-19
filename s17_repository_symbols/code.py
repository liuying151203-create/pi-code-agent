#!/usr/bin/env python3
"""s17：仓库系统 · 符号索引与代码实体。

检索与读取绑定同一目标，通过现有扩展 API 接入；完整 agent_loop 留在本章供阅读。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

# 来自 s15：保持；模型、会话、压缩、事件、结构化工具结果沿用已有组件。
from s05_session_persistence import code as s05
from s11_runtime_observability import code as runtime
from s13_a_sandbox_copy.sandbox import SandboxError
from s15_stateful_extensions import code as previous
from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.result import DispatchTool, ToolResult, execute_tool
from s15_stateful_extensions.session import (
    SessionManager,
    SessionPersistenceError,
    ToolDetails,
    ToolResultSaver,
)
from s16_repository_search.cache_cleanup import run_cache_cleanup

# 来自 s16：保持；检索业务是显式组件依赖，不添加循环参数或全局工作区切换。
from s17_repository_symbols.extension import RepositoryExtension
from s17_repository_symbols.index import RepositoryIndex
from s17_repository_symbols.retrieval import CodeRetriever
from s17_repository_symbols.workspace import RepositoryWorkspace
from s17_repository_symbols.zoekt import DEFAULT_IMAGE, ZoektBackend
from zero2pi.ui import format_error, format_user_prompt

# ===== 来自 s15：通用运行时组件（保持） =====

Message = previous.Message
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
AssistantSaver = previous.AssistantSaver
EventSink = previous.EventSink
AgentEvent = previous.AgentEvent
EventDispatchError = previous.EventDispatchError
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
ContextSummarizer = previous.ContextSummarizer
create_model_provider = previous.create_model_provider
summary_request_options = previous.summary_request_options
show_summary_diagnostic = previous.show_summary_diagnostic
model_round_limit_from_env = previous.model_round_limit_from_env
_positive_int_env = previous._positive_int_env
_get = previous._get
_tool_result_is_error = previous._tool_result_is_error

# 来自 s16：保持；相对 s15，不加载目标外的技能；提示词只描述任务，不描述 Harness 内部机制。
SYSTEM = "你是只读代码仓库助手。根据任务定位代码，用源码证据回答；闲聊不调用工具。"
HARNESS_ROOT = Path(__file__).resolve().parents[1]
# 来自 s16：保持；本章默认仅三种基础读取工具；search_code 由扩展注册。
TOOLS = previous.permissions.READ_ONLY_TOOLS


# ===== 来自 s16：保持；目标选择与独立检索入口 =====


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """解析目标和运行方式，不索引、不调用模型、不创建运行记录。

    输入：可选 workspace/session、--search 查询，或 --clean-indexes 独立清理。
    输出：Namespace；--help 在所有运行组件创建之前退出。
    流程：验证互斥参数 → main 选择清理、直接检索或 Agent 交互；模型工具始终只读。
    """
    parser = argparse.ArgumentParser(description="s17：仓库系统 · 符号索引与代码实体（只读）")
    # 来自 s16：保持；两个章节默认使用同一独立示例，便于对照文本和符号检索。
    default_workspace = HARNESS_ROOT / "example_project" / "retrieval_demo"
    parser.add_argument(
        "--workspace",
        type=Path,
        default=default_workspace,
        help="已有目标项目目录（默认 example_project/retrieval_demo）",
    )
    parser.add_argument("--session", type=Path, help="恢复同一目标的 s17 会话")
    parser.add_argument("--zoekt-image", default=DEFAULT_IMAGE, help="预先构建的本地镜像")
    parser.add_argument("--search", help="直接检索，不读取模型配置、不创建会话")
    parser.add_argument("--scope", default="", help="目标内的相对目录，例如 math_ops")
    parser.add_argument("--limit", type=int, default=10, help="直接检索返回文件数，1–20")
    # 来自 s16：保持；清理只由用户预览、确认，不作为模型工具注册。
    parser.add_argument("--clean-indexes", action="store_true", help="预览并确认清理本目标的旧代次")
    parser.add_argument("--keep-indexes", type=int, help="清理时额外保留最近 N 个有效代次，默认 1")
    # s17 新增：直接检索也能验证符号模式；交互时模式由 search_code 参数指定。
    parser.add_argument(
        "--search-mode", choices=("text", "symbol"), help="直接检索的模式，默认 text"
    )
    options = parser.parse_args(arguments)
    if options.clean_indexes and (options.search is not None or options.session is not None):
        parser.error("--clean-indexes 不能与 --search 或 --session 同用")
    if options.keep_indexes is not None and (not options.clean_indexes or options.keep_indexes < 1):
        parser.error("--keep-indexes 只用于 --clean-indexes，且必须至少为 1")
    options.keep_indexes = options.keep_indexes if options.keep_indexes is not None else 1
    if options.search is None and options.search_mode is not None:
        parser.error("--search-mode 只用于 --search")
    options.search_mode = options.search_mode or "text"
    if options.search is None and (options.scope or options.limit != 10):
        parser.error("--scope 和 --limit 只用于 --search；交互时由工具参数指定")
    if options.search is not None and options.session is not None:
        parser.error("--search 不加载会话")
    return options


# ===== 来自 s15：完整核心循环（保持） =====
# 来自 s16：保持；检索能力通过 tools/dispatch 接入，下面的循环没有检索专用分支。


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
        # 来自 s15：保持；元数据按调用 ID 保存，事件暂存到批次成功落盘之后。
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
                # 来自 s15：保持；错误也使用统一结果对象，但没有成功状态快照。
                result = ToolResult("Error: tool execution stopped; this tool was not executed")
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
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": result.content}
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


# ===== 来自 s16：保持；按目标、检索组件、注册层、模型运行时显式组装 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动直接检索或只读仓库定位 Agent。

    输入：目标项目、镜像和可选历史，或 --search / --clean-indexes 独立入口。
    输出：检索结果和指标；交互时保存会话、Trace；清理时预览并等待用户确认。
    流程：绑定目标 → 清理入口独立退出；否则组装检索、扩展和运行时 → 查询或交互。
    边界：不下载、不构建镜像、不修改源码；未准备 Zoekt 时明确停止，不偷偷退回 grep。
    """
    options = parse_args(arguments)

    # ===== 来自 s16：保持；基础依赖与检索组件 =====
    workspace = RepositoryWorkspace(options.workspace, HARNESS_ROOT)
    # 来自 s16：保持；复用清理流程，但工作区已经绑定 s17，不操作其他章节缓存。
    if options.clean_indexes:
        run_cache_cleanup(workspace, options.keep_indexes)
        return
    # 来自 s16：保持；先验证目标缓存可写，失败时不启动后端、模型或会话。
    workspace.prepare_index_directory()
    # 来自 s16：保持；后端调用 Zoekt，管理器准备缓存，检索器组织一次查询；不把三者都叫索引。
    zoekt_backend = ZoektBackend(options.zoekt_image)
    zoekt_backend.check_ready()
    index_manager = RepositoryIndex(workspace=workspace, backend=zoekt_backend)
    # s17 修改：索引管理器同时提供分片和实体目录，检索器关联命中；组装参数不增加。
    retriever = CodeRetriever(workspace=workspace, index_manager=index_manager)

    # 来自 s16：保持；先单独验证检索链路，不用模型工具行为代替索引正确性测试。
    if options.search is not None:
        # s17 修改：将命令行模式显式传入检索器；其他扫描、缓存和版本校验流程不变。
        result = retriever.search(
            options.search, scope=options.scope, limit=options.limit, mode=options.search_mode
        )
        print(result.content)
        # 来自 s16：保持；直接检索的指标走 stderr，stdout 继续只放结果 JSON。
        print(
            json.dumps({"retrieval_metrics": result.details}, ensure_ascii=False), file=sys.stderr
        )
        if result.content.startswith("Error:"):
            raise SystemExit(1)
        return

    # ===== 来自 s16：保持；会话和基础读取同时绑定目标，注册组件仍沿用 s15 =====
    session_path = workspace.select_session(options.session)
    session = SessionManager.open(session_path)
    trace_path = runtime.new_trace_path(workspace.trace_root)
    metrics_path = trace_path.with_suffix(".retrieval.jsonl")
    extensions = ExtensionAPI(
        base_tools=workspace.tools,
        base_dispatch=workspace.dispatch,
        permission_hook=workspace.permission_hook,
        session=session,
    )
    # 来自 s16：保持；显式注入后 register，不修改 s14 的模块加载器为隐式注入器。
    retrieval_extension = RepositoryExtension(retriever=retriever, metrics_path=metrics_path)
    retrieval_extension.register(extensions)

    # ===== 来自 s15：模型与观察组件（保持） =====
    # 来自 s16：保持；配置固定从 Harness 读取；外部目标的 .env 不作为 Agent 配置执行。
    load_dotenv(HARNESS_ROOT / ".env", override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
    provider = create_model_provider(timeout_seconds=timeout_seconds, max_retries=max_retries)
    terminal = TerminalEventSink()
    trace_recorder = JsonlTraceRecorder(trace_path)
    usage_tracker = UsageTracker()
    error_recorder = SessionErrorRecorder(session)
    events = EventDispatcher()
    events.subscribe(terminal)
    events.subscribe(trace_recorder)
    events.subscribe(usage_tracker)
    events.subscribe(error_recorder)
    events.subscribe(show_summary_diagnostic)
    # 来自 s15：保持；指标观察也在工具批次保存后触发，观察失败仍停止。
    events.subscribe(extensions.handle_event)
    summary_requester = BlockingModelRequester(provider, timeout_seconds, events.emit)
    assistant_requester = StreamingModelRequester(provider, timeout_seconds, events.emit)

    # ===== 来自 s15：上下文与运行时组装（保持） =====
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
    active_tools = extensions.tools
    hooks = extensions.hooks
    round_limit = model_round_limit_from_env()
    # 来自 s16：保持；所有工具路径都相对此目标，不复用旧提示词中的 Harness 工作目录。
    system_prompt = SYSTEM + f"\n目标项目：{workspace.root}；工具路径相对此目录。"

    # ===== 来自 s16：保持；交互入口显示真实目标与三类记录 =====
    print("s17：仓库系统 · 符号索引与代码实体")
    print(f"目标：{workspace.root}；模型：{provider.model}；模式：read-only")
    # 来自 s16：保持；区分 Harness 的缓存写入与模型对业务源码的只读访问。
    print(f"索引缓存：{workspace.index_root}（业务源码只读）")
    print(f"可用工具：{', '.join(tool['name'] for tool in active_tools)}")
    print(f"会话：{session.path}；Trace：{trace_path}；检索指标：{metrics_path}")
    print("输入任务，输入 q 退出。不会执行 shell 或修改业务源码。\n")

    # ===== 来自 s15：任务保存、循环和用量（保持） =====
    while True:
        try:
            # 来自 s15：保持；只是更换提示符，不改变交互规则。
            query = input(format_user_prompt("s17")).strip()
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
                dispatch=extensions.dispatch,
                system=system_prompt,
                hooks=hooks,
                tools=active_tools,
                emit=events.emit,
                save_message=session.append_message,
                save_assistant=session.append_assistant,
                save_tool_results=session.append_tool_results,
                max_rounds=round_limit,
            )
        except (SandboxError, SessionPersistenceError, EventDispatchError):
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
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
