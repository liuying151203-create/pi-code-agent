#!/usr/bin/env python3
"""s20：任务系统 · Git 上下文。

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
from s16_repository_search.index import RepositoryIndex
from s16_repository_search.zoekt import DEFAULT_IMAGE, ZoektBackend
from s17_semantic_retrieval.embeddings import OpenAIEmbeddingProvider
from s17_semantic_retrieval.semantic import SemanticIndex
from s18_relationship_navigation.index import RelationshipIndex
from s18_relationship_navigation.retrieval import RelationshipRetriever
from s19_retrieval_fusion.evidence import EvidencePipeline, FusionPolicy

# 来自 s16：保持；检索业务是显式组件依赖，不添加循环参数或全局工作区切换。
from s19_retrieval_fusion.extension import RepositoryExtension
from s19_retrieval_fusion.retrieval import CodeRetriever
from s19_retrieval_fusion.service import RepositoryRetrieval
from s20_git_task_context.context import TaskContextBuilder, load_task_material
from s20_git_task_context.git_reader import GitReader
from s20_git_task_context.workspace import RepositoryWorkspace
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


# ===== s20 修改：增加独立任务上下文入口；检索参数来自 s18 保持 =====


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """只解析运行选择，不扫描、联网或创建文件。

    输入：目标、会话或直接查询；输出：经过互斥验证的参数。
    流程：解析 → 验证关键词与模式 → main 显式组装所需通道。
    边界：授权仅对本次进程生效；恢复会话不会恢复源码发送授权。
    """
    # 来自 s18：保持；入口增加关系导航，搜索通道仍沿用 s17。
    # s20 修改：任务资料由入口组装，原检索/关系模式保持。
    parser = argparse.ArgumentParser(description="s20：任务系统 · Git 上下文（只读）")
    # 来自 s16：保持；默认只索引独立示例，不扫描 Harness。
    parser.add_argument(
        "--workspace", type=Path, default=HARNESS_ROOT / "example_project" / "retrieval_demo"
    )
    parser.add_argument("--session", type=Path, help="恢复同一目标的 s20 会话")
    parser.add_argument("--zoekt-image", default=DEFAULT_IMAGE, help="复用 s16 本地镜像")
    parser.add_argument("--search", help="直接检索，不创建会话；semantic 无需 Zoekt")
    parser.add_argument("--scope", default="", help="目标内相对目录")
    parser.add_argument("--limit", type=int, default=10, help="返回 1–20 个文件")
    # 来自 s17：保持；自然语言和代码关键词分别传递，不用聊天模型偷偷改写查询。
    parser.add_argument(
        "--search-mode", choices=("text", "symbol", "semantic", "hybrid"), default="text"
    )
    parser.add_argument(
        "--keyword", action="append", dest="keywords", help="hybrid 的代码关键词，可重复 1–4 次"
    )
    parser.add_argument(
        "--allow-remote-embedding",
        action="store_true",
        help="允许将过滤后的源码块和查询发给配置的 embedding 服务，可能计费",
    )
    # 来自 s18：保持；独立关系验证不需要聊天或 embedding 服务，也不需要 Zoekt。
    parser.add_argument("--related", help="用当前 entity_id 查询一跳关系")
    parser.add_argument("--direction", choices=("incoming", "outgoing"), default="incoming")
    parser.add_argument("--kind", choices=("calls", "imports", "inherits", "all"), default="calls")
    # s20 新增：任务资料与基线由用户显式指定；模型没有远程抓取或配置入口。
    parser.add_argument("--task-file", type=Path, help="显式提供 title/body/comments JSON")
    parser.add_argument("--base", help="本地分支、标签或提交；默认 HEAD")
    parser.add_argument(
        "--show-context", action="store_true", help="只显示任务上下文，不用模型/Zoekt"
    )
    parser.add_argument("--task", help="--show-context 的自然语言任务")
    options = parser.parse_args(arguments)
    if (
        options.search is None
        and options.related is None
        and (
            options.scope
            or options.limit != 10
            or options.keywords
            or options.search_mode != "text"
        )
    ):
        parser.error("直接查询参数只能配合 --search；交互时由工具指定")
    if options.search is not None and options.session is not None:
        parser.error("--search 不加载会话")
    if options.related is not None and (
        options.search is not None
        or options.session is not None
        or options.keywords
        or options.search_mode != "text"
        or options.scope
        or options.allow_remote_embedding
    ):
        parser.error("--related 不与搜索/会话/embedding 参数同用")
    if options.related is None and (options.direction != "incoming" or options.kind != "calls"):
        parser.error("--direction/--kind 只用于 --related")
    if options.show_context:
        if not options.task or options.search is not None or options.related or options.session:
            parser.error("--show-context 需要 --task，且不与查询/历史同时使用")
    elif options.task is not None:
        parser.error("--task 只用于 --show-context；交互时直接输入任务")
    if (options.search is not None or options.related) and (options.task_file or options.base):
        parser.error("独立检索不接受任务资料/base")
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
    """预览任务上下文，或用任务资料驱动只读仓库 Agent。

    输入：目标、显式任务资料/基线、可选历史、查询模式和 embedding 授权。
    输出：任务 JSON 或检索结果；交互时保存任务、会话与 Trace。
    流程：绑定目标 → 任务组件/独立预览 → 召回组件 → 扩展/运行时组装 → 交互。
    边界：不下载、不构建镜像、不修改源码；未准备 Zoekt 时明确停止，不偷偷退回 grep。
    """
    options = parse_args(arguments)

    # ===== 来自 s17：保持；基础依赖与召回组件 =====
    workspace = RepositoryWorkspace(options.workspace, HARNESS_ROOT)
    # s20 新增：先组装只读任务组件；独立上下文入口不创建模型、会话或索引。
    task_material = load_task_material(options.task_file)
    git_reader = GitReader(workspace.root)
    task_builder = TaskContextBuilder(
        workspace=workspace, git_reader=git_reader, base=options.base, material=task_material
    )
    if options.show_context:
        task_context = task_builder.build(options.task)
        print(task_context.message()["content"])
        return
    workspace.prepare_index_directory()
    # 来自 s16：保持；只读 Harness 配置，不加载目标仓库的 .env。
    load_dotenv(HARNESS_ROOT / ".env", override=True)
    # 来自 s18：保持；关系组件单独组装，既可直接验证，也可并列注册为工具。
    relationship_index = RelationshipIndex(workspace=workspace)
    relationship_retriever = RelationshipRetriever(
        workspace=workspace, relationship_index=relationship_index
    )
    if options.related is not None:
        result = relationship_retriever.related(
            options.related, direction=options.direction, kind=options.kind, limit=options.limit
        )
        print(result.content)
        print(
            json.dumps({"retrieval_metrics": result.details}, ensure_ascii=False), file=sys.stderr
        )
        if result.content.startswith("Error:"):
            raise SystemExit(1)
        return

    index_manager = None
    if options.search is None or options.search_mode != "semantic":
        zoekt_backend = ZoektBackend(options.zoekt_image)
        zoekt_backend.check_ready()
        index_manager = RepositoryIndex(workspace=workspace, backend=zoekt_backend)

    # 来自 s17：保持；授权与聊天配置分离；没有标志就不创建 embedding 客户端。
    semantic_index = None
    if options.allow_remote_embedding:
        embedding_provider = OpenAIEmbeddingProvider.from_env()
        semantic_index = SemanticIndex(index_root=workspace.index_root, provider=embedding_provider)
        print(
            "检索  已允许向配置的 embedding 服务发送合规源码块和查询（可能计费）。", file=sys.stderr
        )
    # 来自 s19：保持：策略 → 证据管线 → 搜索检索器 → 统一服务，组装层次就地可见。
    fusion_policy = FusionPolicy()
    evidence_pipeline = EvidencePipeline(policy=fusion_policy)
    retriever = CodeRetriever(
        workspace=workspace,
        index_manager=index_manager,
        semantic_index=semantic_index,
        evidence_pipeline=evidence_pipeline,
        relationship_index=relationship_index,
    )
    retrieval_service = RepositoryRetrieval(
        search_retriever=retriever, relationship_retriever=relationship_retriever
    )

    # 来自 s17：保持；直接语义测试不创建聊天模型、Session 或 Trace；指标仍单独输出。
    if options.search is not None:
        result = retriever.search(
            options.search,
            scope=options.scope,
            limit=options.limit,
            mode=options.search_mode,
            keywords=options.keywords,
        )
        print(result.content)
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
    # 来自 s17：保持；搜索扩展负责原来的召回与指标。
    retrieval_extension = RepositoryExtension(
        retriever=retrieval_service, metrics_path=metrics_path
    )
    retrieval_extension.register(extensions)

    # ===== 来自 s15：模型与观察组件（保持） =====
    # 来自 s16：保持；配置固定从 Harness 读取；外部目标的 .env 不作为 Agent 配置执行。
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
    # 来自 s18：保持；显示本章名称；工具目录中可直接观察新增 related_code。
    # s20 修改：入口显示任务上下文章节，避免复用标签误导运行对象。
    print("s20：任务系统 · Git 上下文")
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
            query = input(format_user_prompt("s20")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if query.lower() in {"", "q", "exit"}:
            return
        # s20 新增：先构建/复核任务，再保存；失败时不向模型发送不完整的 Git 资料。
        try:
            task_context = task_builder.build(query)
        except (RuntimeError, ValueError, OSError) as error:
            print(format_error(str(error)), file=sys.stderr)
            continue
        session.append_message(task_context.message())
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
