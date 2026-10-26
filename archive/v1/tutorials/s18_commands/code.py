#!/usr/bin/env python3
"""s18：交互命令注册、持久化与模型上下文。

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
from uuid import uuid4

# ===== 来自 s17：保持；复用已提取到正式包的基础组件，不改变消息和工具协议 =====
from pi_code_agent import serialization as s05

# 来自 s17：保持；默认提示词只复用正式包的同一文本，不引入资源自动加载。
from pi_code_agent.agent import DEFAULT_SYSTEM

# s18 新增：教程和正式入口共用命令注册、结果与记录格式。
from pi_code_agent.command_entry import CommandEntry
from pi_code_agent.commands import CommandContext, CommandRegistry, CommandResult, CommandUI
from pi_code_agent.compaction_base import CompactionPolicy

# 来自 s17：保持；教程与正式入口共用同一解析和校验组件。
from pi_code_agent.config import (
    RunConfig,
    load_environment,
    model_config_from_env,
    run_config_from_env,
    validate_timeout,
)
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
from pi_code_agent.extension_entries import CustomEntry, CustomMessageEntry
from pi_code_agent.extensions.todo import register as register_todo
from pi_code_agent.hooks import BeforeToolHook, Hooks

# 来自 s17：保持；将工作区交给 LocalTools 实例，避免 s15 的模块级路径绑定影响其他 Agent。
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


# ===== 来自 s17：保持；完整循环继续显式展开，便于追踪请求、工具与保存顺序 =====
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
    # 来自 s17：保持；单独注入工具批次保存回调，正文和 details 同时落盘。
    save_tool_results: ToolResultSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
    # 来自 s17：保持；限轮仅在调用方明确配置时启用，不是默认完成策略。
    max_rounds: int | None = None,
) -> list[Message]:
    """运行带工具调用、会话保存和事件观察的 Agent 循环。

    输入：模型消息、请求器、工具分发器、system、Hooks 和保存回调。
    输出：本次运行的完整消息；没有工具调用或模型返回失败状态时结束，超限时报错。
    流程：请求模型 → 保存 assistant → 执行工具 → 保存正文与状态 → 发送结果事件 → 再次请求。
    带 details 的结果必须有专用保存回调；保存失败不发送成功事件，后端失败仍补齐配对。
    """
    # 来自 s17：保持；轮数只计算回答请求；摘要由请求包装器负责，不隐含在计数中。
    # 来自 s17：保持；默认不限制轮数，有明确配置时才停止。
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max_rounds 必须为正整数")
    # 来自 s17：保持；只有 None 回退默认工具，显式空列表不扩大能力。
    active_tools = TOOLS if tools is None else tools
    emit(AgentEvent(type="agent_start", data={"message_count": len(messages)}))

    # 来自 s17：保持；显式统计回答请求，None 时不设置硬停止点。
    round_count = 0
    while max_rounds is None or round_count < max_rounds:
        round_count += 1
        # 来自 s17：保持；摘要或回答请求失败同样结束本次任务，观察者不会收到悬空生命周期。
        try:
            response = create_message(
                system=system,
                messages=messages,
                tools=active_tools,
                max_tokens=max_tokens,
            )
        except EventDispatchError:
            # 来自 s17：保持；观察链失败后不再次广播结束事件。
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
            # 来自 s17：保持；沿用正式包已有的失败状态保护；SDK 必须抛错，调用方才能区分失败与成功。
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
        # 来自 s17：保持；元数据按调用 ID 保存，事件暂存到批次成功落盘之后。
        tool_details: ToolDetails = {}
        result_events: list[AgentEvent] = []
        # 来自 s17：保持；致命后端失败后补齐剩余工具结果，但不继续执行。
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
                # 来自 s17：保持；错误也使用统一结果对象，但没有成功状态快照。
                result = ToolResult("Error: tool execution stopped; this tool was not executed")
            else:
                try:
                    # 来自 s17：保持；已有 Hook 仍处理正文，结构化结果另外携带状态。
                    result = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
                except (SandboxError, KeyboardInterrupt) as error:
                    backend_failure = str(error) or "用户中断工具执行"
                    result = ToolResult(f"Error: {backend_failure}")
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000

            # 来自 s17：保持；模型消息保持标准 tool_result 字段，details 不进入模型协议。
            results.append(
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": result.content}
            )
            if result.details is not None:
                # 来自 s17：保持；名称和调用 ID 来自本次调用，不从模型结果正文猜测。
                tool_details[tool_call_id] = {"name": name, "details": result.details}
            # 来自 s17：保持；沿用已有事件名，结果事件稍后发送；每次调用仍只计数一次。
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
        # 来自 s17：保持；先保存完整批次，再更新内存上下文和通知观察者，避免显示未保存进度。
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
        # 来自 s17：保持；使用各次结果的独立快照，不能把同批较早事件都显示为最终状态。
        for event in result_events:
            emit(event)
        # 来自 s13-A：保持；先保存整个批次，再终止；加载历史时不会留下缺失 tool_result。
        if backend_failure is not None:
            emit(
                AgentEvent(
                    # 来自 s17：保持；通用分发器的失败事件不声称本次使用了沙箱。
                    type="agent_end",
                    data={"messages": list(messages), "reason": "tool_backend_failed"},
                )
            )
            raise SandboxError(backend_failure)

    # 来自 s17：保持；保留已完成工具结果并报告未完成，不伪造一次模型最终回答。
    emit(
        AgentEvent(
            type="agent_end",
            data={"messages": list(messages), "reason": "max_rounds"},
        )
    )
    raise RuntimeError(f"已达到 {max_rounds} 轮模型请求上限，任务尚未完成；已停止自动调用工具。")


# ===== s18 修改：在同步 SDK 增加命令执行入口，完整模型循环保持 s17 =====
# s18 修改：命令与 run 共享空闲检查、Session、事件和扩展，不另建核心循环。
class Agent:
    """将一份会话的模型、工具、存储与观察链组装成独立实例。

    输入：Provider、工作区，以及可替换的后端、权限、存储和压缩配置。
    输出：run 返回模型上下文；execute_command 返回命令结果；观察和扩展使用同一实例。
    流程：绑定会话 → 创建命令与事件链 → 组装请求器 → 保存任务或命令并执行。
    边界：同步单任务，不承诺崩溃后重放工具，不自动安装或加载不可信模块。
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        workspace: Path,
        data_dir: Path | None = None,
        session_path: Path | None = None,
        mode: str = "read-only",
        tools: list[dict[str, Any]] | None = None,
        # 来自 s17：保持；用已有分发与权限类型说明注入契约，不改变后端行为。
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
        # 来自 s17：保持；显式 SDK 参数也先统一校验，避免非法额度在保存任务后才被发现。
        run_config = RunConfig(
            max_rounds=max_rounds,
            max_tokens=max_tokens,
            compaction_policy=compaction_policy,
            summary_max_tokens=summary_max_tokens,
        )
        validate_timeout(timeout_seconds)
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir() or self.workspace == Path(self.workspace.anchor):
            raise ValueError("工作区必须是已有项目目录，不能是磁盘根目录")
        workspace_id = hashlib.sha256(os.path.normcase(str(self.workspace)).encode()).hexdigest()[
            :16
        ]
        records = (
            Path(data_dir) if data_dir is not None else self.workspace / ".pi-code-agent"
        ).resolve()
        custom = (tools is not None, dispatch is not None, permission_hook is not None)
        if any(custom) and not all(custom):
            raise ValueError("自定义后端必须同时提供 tools、dispatch 和 permission_hook")
        local = None if all(custom) else LocalTools(self.workspace, mode)
        if local is not None:
            # 产品提取新增：外部指定的记录目录即使在工作区内，也不能经模型工具读取。
            local.protected_roots = (records,)
        chosen_tools = tools if all(custom) else local.tools
        chosen_dispatch = dispatch if all(custom) else local.dispatch
        chosen_permission = permission_hook if all(custom) else local.permission_hook

        session_root = records / "sessions" / workspace_id
        if session_path is not None and Path(session_path).resolve().parent != session_root:
            raise ValueError("会话必须属于当前 data_dir 和工作区")
        selected = select_session_path(
            Path(session_path) if session_path else None, session_root=session_root
        )
        self.session = SessionManager.open(selected)
        self.events = EventDispatcher()
        self.usage = UsageTracker()
        self.trace_path = new_trace_path(records / "traces" / workspace_id)
        # s18 新增：内置和扩展共享命令表，帮助始终来自本实例真实注册的能力。
        self.commands = CommandRegistry()
        self.command_ui = CommandUI(self.events.emit)
        self.commands.register("help", self.commands.help, description="查看命令帮助")
        self.commands.register("stats", self._stats_command, description="查看当前会话的模型用量")
        self.extensions = ExtensionAPI(
            base_tools=chosen_tools,
            base_dispatch=chosen_dispatch,
            permission_hook=chosen_permission,
            session=self.session,
            commands=self.commands,
            actions=self,
        )
        self.events.subscribe(SessionErrorRecorder(self.session))
        self.events.subscribe(JsonlTraceRecorder(self.trace_path))
        self.events.subscribe(self.usage)
        self.events.subscribe(self.extensions.handle_event)

        assistant_request = StreamingModelRequester(provider, timeout_seconds, self.events.emit)
        self.requester = assistant_request
        # 来自 s17：保持；请求链只使用已统一校验的压缩策略和摘要额度。
        if run_config.compaction_policy is not None:
            summary_request = BlockingModelRequester(provider, timeout_seconds, self.events.emit)
            summarizer = ContextSummarizer(
                create_message=summary_request,
                session=self.session,
                max_tokens=run_config.summary_max_tokens,
                emit=self.events.emit,
                request_options=summary_request_options(provider),
            )
            compactor = ContextCompactor(self.session, run_config.compaction_policy, summarizer)
            self.requester = CompactedContextRequester(
                assistant_request, self.session, compactor, self.events.emit
            )
        self.system = system
        # 来自 s17：保持；使用统一校验后的额度，循环及其计数语义保持 s16 行为。
        self.max_tokens = run_config.max_tokens
        self.max_rounds = run_config.max_rounds
        self._running = False
        self._closed = False
        # s18 新增：同步 handler 的触发意图由外层统一兑现，不重入 run。
        self._extension_callback_active = False
        self._turn_requested = False

    # 来自 s17：保持；公开事件订阅的实际输入类型，继续复用 s08/s11 的分发规则。
    def subscribe(self, listener: EventSink) -> None:
        """把调用方观察者加入本实例的事件链。

        输入：listener(event)；输出：None，不返回取消订阅句柄。
        流程：交给 EventDispatcher 注册；运行时由 emit 按顺序通知，观察者失败则停止。
        """
        self.events.subscribe(listener)

    # 来自 s17：保持；补全可信注册的输入、输出和空闲时序说明，注册规则保持不变。
    def register_extension(self, register: Callable[[ExtensionAPI], None]) -> None:
        """在空闲实例上注册调用方选择的可信能力。

        输入：register(api)；输出：None，能力加入当前 ExtensionAPI。
        流程：检查关闭和运行标记 → 调用注册函数，函数可从会话恢复持久化工具状态。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或正在运行，不能更改能力")
        register(self.extensions)

    # 来自 s17：保持；用已有 Message 类型明确返回的是有效模型上下文，而非完整存储日志。
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

    # ===== s18 新增：Pi 风格的命令效果与同步模型回合 =====
    def execute_command(self, text: str) -> CommandResult:
        """执行 handler(args, ctx)，保存审计后按显式意图启动模型。

        输入：以 / 开头的命令和原样参数文字；输出：命令处理状态。
        流程：保存开始 → handler 选择通知/状态/消息 → 保存结束 → 按需执行同一循环。
        边界：命令审计不进上下文；效果已保存就不回滚；模型失败向调用方抛出。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if not isinstance(text, str) or not text.strip().startswith("/"):
            raise ValueError("交互命令必须以 / 开头")
        command = text.strip()
        command_id = uuid4().hex
        self._running = True
        self._turn_requested = False
        try:
            try:
                self.session.append_command(CommandEntry(command_id, command, "started"))
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._closed = True
                raise SessionPersistenceError(
                    f"命令开始记录保存失败，未执行命令：{error}"
                ) from error
            self.events.emit(
                AgentEvent("command_start", {"command_id": command_id, "command": command})
            )
            started_at = perf_counter()
            parts = command[1:].split(maxsplit=1)
            definition = self.commands.get(parts[0]) if parts else None
            args = parts[1] if len(parts) == 2 else ""
            result = CommandResult()
            if definition is None:
                result = CommandResult(True, f"未知命令：{command}。输入 /help 查看可用命令。")
            else:
                ctx = CommandContext(self.command_ui, self.session)
                self._extension_callback_active = True
                try:
                    returned = definition.handler(args, ctx)
                    if returned is not None:
                        raise TypeError("命令 handler 应通过效果 API 输出并返回 None")
                except (EventDispatchError, SessionPersistenceError, SandboxError):
                    raise
                except Exception as error:  # noqa: BLE001 - 普通 handler 失败保存审计
                    result = CommandResult(True, f"命令 /{definition.name} 执行失败：{error}")
                finally:
                    self._extension_callback_active = False
            status = "failed" if result.is_error else "succeeded"
            try:
                self.session.append_command(CommandEntry(command_id, command, status, result.error))
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._closed = True
                raise SessionPersistenceError(f"命令结果未可靠保存，已关闭入口：{error}") from error
            self.events.emit(
                AgentEvent(
                    "command_end",
                    {
                        "command_id": command_id,
                        "command": command,
                        "status": status,
                        "duration_ms": (perf_counter() - started_at) * 1000,
                    },
                )
            )
            if result.is_error:
                self.command_ui.notify(result.error, "error")
            elif self._turn_requested:
                # s18 新增：参考 Pi triggerTurn；同步实现延后到 handler 和审计结束，防止重入。
                agent_loop(
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
            return result
        finally:
            self._running = False
            self._extension_callback_active = False
            self._turn_requested = False

    # s18 新增：扩展私有状态与模型上下文独立保存。
    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """保存扩展状态，不发模型消息或启动回合。

        输入：类型与 JSON 数据；输出：无；流程：时序检查 → 校验复制 → 持久化。
        边界：私有状态可在运行期间追加，不打断模型协议；存储失败关闭入口。
        """
        if self._closed:
            raise RuntimeError("Agent 已关闭或已有任务运行")
        entry = CustomEntry(custom_type, data)
        try:
            self.session.append_extension_entry(entry)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._closed = True
            raise SessionPersistenceError(f"扩展状态保存失败，已关闭入口：{error}") from error

    # s18 新增：展示与回合触发独立控制，正文显式进入上下文。
    def send_message(
        self,
        content: str,
        *,
        custom_type: str = "extension",
        display: bool = True,
        details: Any = None,
        trigger_turn: bool = False,
    ) -> None:
        """保存模型可见消息，分别控制界面展示与模型回合触发。

        输入：文本、类型、展示/触发开关及 JSON 元数据；输出：无。
        流程：校验 → 保存 → 发送展示事件 → 命令内记录触发意图，空闲时直接运行。
        边界：不支持运行期间投递；details 不进入模型，display 不控制模型可见性。
        """
        if self._closed or (self._running and not self._extension_callback_active):
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if type(trigger_turn) is not bool:
            raise TypeError("trigger_turn 必须是布尔值")
        entry = CustomMessageEntry(custom_type, content, display, details)
        owns_operation = not self._running
        if owns_operation:
            self._running = True
        try:
            try:
                self.session.append_extension_entry(entry)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._closed = True
                raise SessionPersistenceError(f"扩展消息保存失败，已关闭入口：{error}") from error
            self.events.emit(
                AgentEvent(
                    "custom_message",
                    {
                        "custom_type": custom_type,
                        "content": content,
                        "display": display,
                    },
                )
            )
            if self._extension_callback_active:
                self._turn_requested |= trigger_turn
            elif trigger_turn:
                agent_loop(
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
            if owns_operation:
                self._running = False

    # s18 新增：真实用户消息请求模型回合，受控回调中先保存后触发。
    def send_user_message(self, content: str) -> None:
        """发送真实用户任务，始终请求模型回合。

        输入：非空用户文本；输出：无；流程：空闲时调用 run，命令内保存并延后触发。
        边界：命令期间不会重入 run；失败 handler 不兑现已请求的回合，已保存消息仍保留。
        """
        if self._closed or (self._running and not self._extension_callback_active):
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("任务不能为空")
        if self._extension_callback_active:
            try:
                self.session.append_message({"role": "user", "content": content})
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._closed = True
                raise SessionPersistenceError(f"用户消息保存失败，已关闭入口：{error}") from error
            self._turn_requested = True
        else:
            self.run(content)

    def _stats_command(self, args: str, ctx: CommandContext) -> None:
        """展示执行当时的累计统计，不向模型发送消息。

        输入：空参数和命令上下文；输出：通知；流程：拒绝参数 → 读取统计 → 展示。
        """
        if args:
            raise ValueError("/stats 不接受参数")
        ctx.ui.notify(ctx.session.session_stats().summary_text())

    # 来自 s17：保持；明确关闭入口与外部资源所有权，保留已有同步生命周期。
    def close(self) -> None:
        """关闭空闲实例的任务入口，不代管外部资源。

        输入：无；输出：None；流程：拒绝运行中关闭 → 标记实例已关闭。
        外部 Provider 和执行后端仍由创建者关闭，本方法不提供并发取消。
        """
        if self._running:
            raise RuntimeError("同步任务尚未结束，不能关闭")
        self._closed = True


# 来自 s17：保持；工厂显式列出已有参数，调用者无需穿透 **kwargs 查找 SDK 契约。
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


# ===== s18 修改：终端输入接入同一命令入口 =====


# 来自 s17：保持；用快照替代 s16 分散的环境读取，完整循环继续保留。
def main(arguments: Sequence[str] | None = None) -> None:
    """读取配置快照并校验，再组装 SDK、注册能力和进入终端对话。

    输入：启动选项、启动目录 .env 和进程环境；输出：终端回答与持久化记录。
    流程：选项 → 配置快照 → 模型与运行配置 → Provider/Agent → 交互并关闭。
    边界：不修改 os.environ，不从目标项目加载宿主配置，不自动安装依赖。
    """
    # 来自 s17：保持；只解析原有五种选项，帮助与非法参数提前退出。
    parser = argparse.ArgumentParser(description="s18：交互命令与会话历史")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--data-dir", type=Path, default=Path.cwd() / ".pi-code-agent/tutorials/s18"
    )
    parser.add_argument("--session", type=Path)
    parser.add_argument("--mode", choices=("read-only", "default"), default="read-only")
    parser.add_argument("--extension", action="append", default=[], metavar="MODULE")
    options = parser.parse_args(arguments)

    # 来自 s17：保持；文件位置固定、环境优先；先解析两组配置，失败不创建模型或会话。
    environment = load_environment(Path.cwd() / ".env", environ=os.environ)
    model_config = model_config_from_env(environment)
    run_config = run_config_from_env(environment, enable_compaction=True)

    # 来自 s17：保持；功能组件消费已校验配置，真实 HTTP 超时由模型客户端应用。
    # 来自 s17：保持；技能由调用方显式扫描，不增加多来源加载机制。
    workspace = options.workspace.resolve(strict=True)
    # 来自 s17：保持；扫描边界使用已选择工作区，避免启动目录不同导致已有能力不可用。
    skill_catalog = SkillCatalog.scan(workspace / "skills", workspace=workspace)
    system_prompt = build_system_prompt(SYSTEM, skill_catalog)
    provider = create_model_provider(config=model_config)
    terminal = TerminalEventSink()

    # 来自 s17：保持；回答额度与轮数接入 SDK，摘要策略继续沿用已有实现。
    agent = create_agent(
        provider=provider,
        workspace=workspace,
        data_dir=options.data_dir,
        session_path=options.session,
        mode=options.mode,
        compaction_policy=run_config.compaction_policy,
        summary_max_tokens=run_config.summary_max_tokens,
        timeout_seconds=model_config.timeout_seconds,
        max_tokens=run_config.max_tokens,
        max_rounds=run_config.max_rounds,
        system=system_prompt,
    )
    try:
        # 来自 s17：保持；可信扩展和终端仍由入口显式注册，不自动发现。
        if options.extension:
            load_extensions(agent.extensions, options.extension)
        else:
            agent.register_extension(register_todo)
        agent.subscribe(terminal)
        # s18 修改：显示程序命令入口，--help 仍由 argparse 处理。
        print(
            f"工作区：{agent.workspace}\n会话：{agent.session.path}\n输入任务，/help 查看命令，q 退出。"
        )
        while True:
            try:
                prompt = input(format_user_prompt("s18")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in {"", "q", "exit"}:
                break
            # s18 新增：参考 lcc 的 REPL 先分发命令；改用共享 SDK 入口以保存历史、支持程序调用。
            if prompt.startswith("/"):
                agent.execute_command(prompt)
                continue
            agent.run(prompt)
            print(agent.session.session_stats().summary_text())
    finally:
        # 来自 s17：保持；配置接线后的注册失败也走已有关闭流程。
        agent.close()


# 来自 s17：保持；错误明确退出。
if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
