#!/usr/bin/env python3
"""s16：从完整章节入口到可复用的同步 Agent SDK。

输入依赖由调用方提供；Agent 负责单份会话的组装和运行，main 负责终端交互。
本章保留完整核心循环；正式应用使用 pi_code_agent，不导入教学快照。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

# ===== 来自 s15：保持；复用已提取到正式包的基础组件，不改变消息和工具协议 =====
from pi_code_agent import serialization as s05

# 来自 s15：保持；默认提示词只复用正式包的同一文本，不引入资源自动加载。
from pi_code_agent.agent import DEFAULT_SYSTEM
from pi_code_agent.compaction_base import CompactionPolicy, _positive_int_env
from pi_code_agent.events import (
    AgentEvent,
    EventDispatcher,
    EventDispatchError,
    EventSink,
    JsonlTraceRecorder,
    TerminalEventSink,
    UsageTracker,
    new_trace_path,
)
from pi_code_agent.extensions.todo import register as register_todo
from pi_code_agent.hooks import BeforeToolHook, Hooks

# s16 新增：将工作区交给 LocalTools 实例，避免 s15 的模块级路径绑定影响其他 Agent。
from pi_code_agent.local_tools import LocalTools
from pi_code_agent.models import ModelProvider, create_model_provider, summary_request_options
from pi_code_agent.observable_session import AssistantSaver, SessionErrorRecorder
from pi_code_agent.permissions import TOOLS
from pi_code_agent.registry import ExtensionAPI, load_extensions
from pi_code_agent.requests import (
    BlockingModelRequester,
    CompactedContextRequester,
    ContextCompactor,
    ContextSummarizer,
    StreamingModelRequester,
    _tool_result_is_error,
    model_round_limit_from_env,
)
from pi_code_agent.result import DispatchTool, ToolResult, execute_tool
from pi_code_agent.sandbox_copy.sandbox import SandboxError
from pi_code_agent.serialization import Message, SessionSaver, _get, select_session_path
from pi_code_agent.session import (
    SessionManager,
    SessionPersistenceError,
    ToolDetails,
    ToolResultSaver,
)
from pi_code_agent.skills import SYSTEM, SkillCatalog, build_system_prompt
from pi_code_agent.ui import format_error, format_user_prompt


# ===== 来自 s15：保持；完整循环继续显式展开，便于追踪请求、工具与保存顺序 =====
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
            # s16 修改：沿用正式包已有的失败状态保护；SDK 必须抛错，调用方才能区分失败与成功。
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


# ===== s16 新增：同步 SDK 的组装、运行与公开工厂 =====
# s16 新增：将 s15 main 的会话和运行组件归入实例，供终端与程序分别调用。
class Agent:
    """将一份会话的模型、工具、存储与观察链组装成独立实例。

    输入：Provider、工作区，以及可替换的后端、权限、存储和压缩配置。
    输出：run 返回本次运行的模型上下文；subscribe 观察事件；register_extension 接入能力。
    流程：绑定会话 → 创建事件链 → 组装请求器 → 注册能力 → 保存任务并执行核心循环。
    边界：同步单任务，不承诺崩溃后重放工具，不自动安装或加载不可信模块。
    """

    # s16 新增：参考 Pi 的会话工厂集中组装依赖，本章保持同步单任务以便逐步学习。
    def __init__(
        self,
        *,
        provider: ModelProvider,
        workspace: Path,
        data_dir: Path | None = None,
        session_path: Path | None = None,
        mode: str = "read-only",
        tools: list[dict[str, Any]] | None = None,
        # s16 修改：用已有分发与权限类型说明注入契约，不改变后端行为。
        dispatch: DispatchTool | None = None,
        permission_hook: BeforeToolHook | None = None,
        compaction_policy: CompactionPolicy | None = None,
        summary_max_tokens: int = 4096,
        timeout_seconds: float = 60,
        max_tokens: int = 8000,
        max_rounds: int | None = None,
        system: str = DEFAULT_SYSTEM,
    ) -> None:
        """将调用方依赖组装成绑定单份会话的实例，不请求模型。

        输入：Provider、已有工作区及可选后端、存储、提示词和压缩参数。
        输出：已打开会话并创建请求链的 Agent。
        流程：检查路径和后端 → 打开会话 → 创建扩展与事件 → 组装请求链。
        自定义后端须同时提供 tools/dispatch/permission_hook，避免只换工具目录却遗漏门禁。
        data_dir 是显式宿主记录目录；外部产品不必写入底座安装位置。
        """
        # s16 新增：工作区、记录路径和生命周期均属于实例，不改变进程 CWD。
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir() or self.workspace == Path(self.workspace.anchor):
            raise ValueError("工作区必须是已有项目目录，不能是磁盘根目录")
        workspace_id = hashlib.sha256(os.path.normcase(str(self.workspace)).encode()).hexdigest()[
            :16
        ]
        records = (
            Path(data_dir) if data_dir is not None else self.workspace / ".pi-code-agent"
        ).resolve()
        # s16 新增：替换后端时同时注入工具、分发和权限，防止遗漏门禁。
        custom = (tools is not None, dispatch is not None, permission_hook is not None)
        if any(custom) and not all(custom):
            raise ValueError("自定义后端必须同时提供 tools、dispatch 和 permission_hook")
        local = None if all(custom) else LocalTools(self.workspace, mode)
        if local is not None:
            # s16 新增：实例记录目录即使在工作区内，也不开放给模型的文件工具。
            local.protected_roots = (records,)
        chosen_tools = tools if all(custom) else local.tools
        chosen_dispatch = dispatch if all(custom) else local.dispatch
        chosen_permission = permission_hook if all(custom) else local.permission_hook

        # s16 新增：记录按工作区隔离；恢复仅接受同一 data_dir 和工作区的会话。
        session_root = records / "sessions" / workspace_id
        if session_path is not None and Path(session_path).resolve().parent != session_root:
            raise ValueError("会话必须属于当前 data_dir 和工作区")
        selected = select_session_path(
            Path(session_path) if session_path else None, session_root=session_root
        )
        self.session = SessionManager.open(selected)
        # 来自 s15：保持；会话先于扩展创建，供注册时恢复 details 状态。
        self.events = EventDispatcher()
        self.usage = UsageTracker()
        self.trace_path = new_trace_path(records / "traces" / workspace_id)
        self.extensions = ExtensionAPI(
            base_tools=chosen_tools,
            base_dispatch=chosen_dispatch,
            permission_hook=chosen_permission,
            session=self.session,
        )
        self.events.subscribe(SessionErrorRecorder(self.session))
        self.events.subscribe(JsonlTraceRecorder(self.trace_path))
        self.events.subscribe(self.usage)
        self.events.subscribe(self.extensions.handle_event)

        # s16 修改：将 s15 main 的请求链移入实例；传入策略才组装已有压缩器。
        assistant_request = StreamingModelRequester(provider, timeout_seconds, self.events.emit)
        self.requester = assistant_request
        if compaction_policy is not None:
            summary_request = BlockingModelRequester(provider, timeout_seconds, self.events.emit)
            summarizer = ContextSummarizer(
                create_message=summary_request,
                session=self.session,
                max_tokens=summary_max_tokens,
                emit=self.events.emit,
                request_options=summary_request_options(provider),
            )
            compactor = ContextCompactor(self.session, compaction_policy, summarizer)
            self.requester = CompactedContextRequester(
                assistant_request, self.session, compactor, self.events.emit
            )
        self.system = system
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        # s16 新增：只防止同步实例重入和关闭后使用，不提供取消或线程调度。
        self._running = False
        self._closed = False

    # s16 修改：公开事件订阅的实际输入类型，继续复用 s08/s11 的分发规则。
    def subscribe(self, listener: EventSink) -> None:
        """把调用方观察者加入本实例的事件链。

        输入：listener(event)；输出：None，不返回取消订阅句柄。
        流程：交给 EventDispatcher 注册；运行时由 emit 按顺序通知，观察者失败则停止。
        """
        self.events.subscribe(listener)

    # s16 新增：调用方显式注册可信能力，组装层不扫描或执行工作区 Python。
    def register_extension(self, register: Callable[[ExtensionAPI], None]) -> None:
        """在空闲实例上注册调用方选择的可信能力。

        输入：register(api)；输出：None，能力加入当前 ExtensionAPI。
        流程：检查关闭和运行标记 → 调用注册函数，函数可从会话恢复持久化工具状态。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或正在运行，不能更改能力")
        register(self.extensions)

    # s16 新增：把 s15 输入循环中的任务保存和 loop 调用收进 run，去掉终端依赖。
    def run(self, prompt: str) -> list[Message]:
        """保存任务、投影会话并运行；失败向调用方传播，不当作成功验收。

        输入：非空任务；输出：包含此前有效历史的本次模型上下文，不等于完整 JSONL 日志。
        流程：检查生命周期 → 追加用户消息 → 构建上下文 → 同一 loop → 释放运行状态。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("任务不能为空")
        self._running = True
        try:
            self.session.append_message({"role": "user", "content": prompt})
            return agent_loop(
                self.session.build_context(),
                create_message=self.requester,
                dispatch=self.extensions.dispatch,
                system=self.system,
                hooks=self.extensions.hooks,
                emit=self.events.emit,
                save_message=self.session.append_message,
                save_assistant=self.session.append_assistant,
                save_tool_results=self.session.append_tool_results,
                tools=self.extensions.tools,
                max_tokens=self.max_tokens,
                max_rounds=self.max_rounds,
            )
        finally:
            self._running = False

    # s16 新增：调用方在 finally 关闭；外部 Provider 和执行后端仍由创建者管理。
    def close(self) -> None:
        """关闭空闲实例的任务入口，不代管外部资源。

        输入：无；输出：None；流程：拒绝运行中关闭 → 标记实例已关闭。
        外部 Provider 和执行后端仍由创建者关闭，本方法不提供并发取消。
        """
        if self._running:
            raise RuntimeError("同步任务尚未结束，不能关闭")
        self._closed = True


# s16 修改：工厂显式列出已有参数，调用者无需穿透 **kwargs 查找 SDK 契约。
def create_agent(
    *,
    provider: ModelProvider,
    workspace: Path,
    data_dir: Path | None = None,
    session_path: Path | None = None,
    mode: str = "read-only",
    tools: list[dict[str, Any]] | None = None,
    dispatch: DispatchTool | None = None,
    permission_hook: BeforeToolHook | None = None,
    compaction_policy: CompactionPolicy | None = None,
    summary_max_tokens: int = 4096,
    timeout_seconds: float = 60,
    max_tokens: int = 8000,
    max_rounds: int | None = None,
    system: str = DEFAULT_SYSTEM,
) -> Agent:
    """用明确的依赖和选项创建一个同步 Agent。

    输入：Provider、已有工作区，以及可选的存储、后端和运行参数。
    输出：已组装、尚未请求模型的 Agent；构造会选择并打开会话。
    流程：参数原样交给 Agent → 返回实例；环境配置和扩展选择仍由调用方负责。
    """
    return Agent(
        provider=provider,
        workspace=workspace,
        data_dir=data_dir,
        session_path=session_path,
        mode=mode,
        tools=tools,
        dispatch=dispatch,
        permission_hook=permission_hook,
        compaction_policy=compaction_policy,
        summary_max_tokens=summary_max_tokens,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
        system=system,
    )


# ===== s16 修改：入口只提供依赖、展示和交互，运行细节交给 Agent =====
# s16 修改：保留 lcc 的逐步可读入口；与其全局客户端不同，本章把依赖传给独立实例。
def main(arguments: Sequence[str] | None = None) -> None:
    """在终端组装并使用本章 SDK，展示从 s15 main 到 Agent 的职责迁移。

    输入：已有模型环境、workspace/data-dir/session/mode/extension 选项和终端任务。
    输出：模型回答、同一会话的状态与记录；运行失败向外传播。
    流程：基础依赖 → 提示词与压缩策略 → 创建 Agent 并注册能力 → 交互并关闭。
    边界：本章只演示调用方显式读取配置；统一解析留到 s17，SDK 不自动读取 .env。
    """
    # s16 修改：先解析输入；--help 或非法参数退出时不创建客户端、会话或 Trace。
    parser = argparse.ArgumentParser(description="s16：从教程到同步 Agent SDK")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--data-dir", type=Path, default=Path.cwd() / ".pi-code-agent/tutorials/s16"
    )
    parser.add_argument("--session", type=Path)
    parser.add_argument("--mode", choices=("read-only", "default"), default="read-only")
    parser.add_argument("--extension", action="append", metavar="MODULE")
    options = parser.parse_args(arguments)

    # ===== 来自 s15：保持；配置仍由入口显式读取，不在 SDK 构造器读环境 =====
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
    provider = create_model_provider(timeout_seconds=timeout_seconds, max_retries=max_retries)

    # s16 修改：基础路径绑定实例；已有技能扫描由调用方显式作用于所选工作区。
    workspace = options.workspace.resolve(strict=True)
    # s16 修复：已选工作区显式传给扫描器，避免与启动目录不一致时拒绝合法技能目录。
    skill_catalog = SkillCatalog.scan(workspace / "skills", workspace=workspace)
    system_prompt = build_system_prompt(SYSTEM, skill_catalog)
    # 来自 s15：保持；沿用模型窗口、预留、近期预算及摘要额度，不新增配置格式。
    policy = CompactionPolicy(
        context_window_tokens=_positive_int_env("MODEL_CONTEXT_WINDOW_TOKENS", 0),
        reserve_tokens=_positive_int_env("SESSION_COMPACTION_RESERVE_TOKENS", 16384),
        keep_recent_tokens=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000),
    )
    summary_max_tokens = _positive_int_env(
        "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", int(policy.reserve_tokens * 0.8)
    )
    round_limit = model_round_limit_from_env()
    terminal = TerminalEventSink()

    # s16 修改：s15 的会话、事件和请求器组装收进 Agent，入口仍明确列出每个依赖。
    agent = create_agent(
        provider=provider,
        workspace=workspace,
        data_dir=options.data_dir,
        session_path=options.session,
        mode=options.mode,
        compaction_policy=policy,
        summary_max_tokens=summary_max_tokens,
        timeout_seconds=timeout_seconds,
        max_rounds=round_limit,
        system=system_prompt,
    )
    try:
        # 来自 s15：保持；默认 Todo，显式 --extension 替换默认，只加载可信模块。
        if options.extension:
            load_extensions(agent.extensions, options.extension)
        else:
            agent.register_extension(register_todo)
        agent.subscribe(terminal)
        print("s16：从教程到同步 Agent SDK")
        print(f"工作区：{agent.workspace}；模式：{options.mode}")
        print(f"会话：{agent.session.path}；Trace：{agent.trace_path}")
        print("输入任务，q 退出；扩展在宿主运行。")

        # s16 修改：交互层只处理用户输入；保存、投影和循环均经同一个 run 入口。
        while True:
            try:
                prompt = input(format_user_prompt("s16")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in {"", "q", "exit"}:
                break
            agent.run(prompt)
            print(agent.session.session_stats().summary_text())
    finally:
        # s16 新增：包含注册和观察者失败路径，调用方都要执行同步关闭。
        agent.close()


# 来自 s15：保持；启动和运行错误清晰退出，不伪装任务成功。
if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
