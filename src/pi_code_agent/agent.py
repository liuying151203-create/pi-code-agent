"""外部应用使用的同步 SDK：显式组装组件，不复制核心循环。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from pi_code_agent.command_entry import CommandEntry
from pi_code_agent.commands import CommandContext, CommandResult, CommandUI
from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.config import RunConfig, validate_timeout
from pi_code_agent.events import (
    AgentEvent,
    EventDispatcher,
    EventDispatchError,
    EventSink,
    JsonlTraceRecorder,
    UsageTracker,
    new_trace_path,
)
from pi_code_agent.extension_entries import CustomEntry, CustomMessageEntry
from pi_code_agent.hooks import BeforeToolHook
from pi_code_agent.local_tools import LocalTools
from pi_code_agent.loop import agent_loop
from pi_code_agent.models import ModelProvider, summary_request_options
from pi_code_agent.observable_session import SessionErrorRecorder
from pi_code_agent.registry import ExtensionAPI
from pi_code_agent.requests import (
    BlockingModelRequester,
    CompactedContextRequester,
    ContextCompactor,
    ContextSummarizer,
    StreamingModelRequester,
)
from pi_code_agent.result import DispatchTool
from pi_code_agent.sandbox_copy.sandbox import SandboxError
from pi_code_agent.serialization import Message, select_session_path
from pi_code_agent.session import SessionManager, SessionPersistenceError

# s16 新增：构造器与工厂共用现有提示词默认值，明确公开参数时避免两处漂移。
DEFAULT_SYSTEM = "你是编程 Agent。仅在任务需要时使用工具，优先专用工具，完成后停止。"


# s16 修改：明确组装层的公开输出与职责，具体运行机制继续复用已有组件。
class Agent:
    """将一份会话的模型、工具、存储与观察链组装成独立实例。

    输入：Provider、工作区、可选 extensions/flag_values，以及后端、权限和压缩配置。
    输出：run 返回模型上下文；execute_command 返回命令结果；观察和扩展使用同一实例。
    流程：绑定扩展与会话 → 创建命令和事件链 → 首次初始化 → 保存任务或命令并执行。
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
        # s16 修改：用已有分发与权限类型说明注入契约，不改变后端行为。
        dispatch: DispatchTool | None = None,
        permission_hook: BeforeToolHook | None = None,
        compaction_policy: CompactionPolicy | None = None,
        summary_max_tokens: int = 4096,
        timeout_seconds: float = 60,
        max_tokens: int = 8000,
        max_rounds: int | None = None,
        system: str = DEFAULT_SYSTEM,
        # s19 新增：调用方可注入已加载的声明及显式参数值，避免二次执行扩展。
        extensions: ExtensionAPI | None = None,
        flag_values: Mapping[str, bool | str] | None = None,
    ) -> None:
        """将调用方依赖组装成绑定单份会话的实例，不请求模型。

        输入：Provider、已有工作区及可选后端、存储、提示词和压缩参数。
        输出：已打开会话并创建请求链的 Agent。
        流程：检查路径和后端 → 打开会话 → 创建扩展与事件 → 组装请求链。
        自定义后端须同时提供 tools/dispatch/permission_hook，避免只换工具目录却遗漏门禁。
        data_dir 是显式宿主记录目录；外部产品不必写入底座安装位置。
        """
        # s17 新增：显式 SDK 参数也先统一校验，避免非法额度在保存任务后才被发现。
        run_config = RunConfig(
            max_rounds=max_rounds,
            max_tokens=max_tokens,
            compaction_policy=compaction_policy,
            summary_max_tokens=summary_max_tokens,
        )
        validate_timeout(timeout_seconds)
        # s19 新增：预加载声明在会话创建前校验；SDK 后续注册的声明在首次启动前校验。
        prepared_extensions = extensions if extensions is not None else ExtensionAPI()
        if prepared_extensions.is_bound:
            raise ValueError("扩展容器已经绑定，不能复用于另一个 Agent")
        if flag_values is not None:
            prepared_extensions.flags.set_values(flag_values)
        if extensions is not None:
            prepared_extensions.flags.validate()
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
        self.commands = prepared_extensions.commands
        self.command_ui = CommandUI(self.events.emit)
        self.commands.register("help", self.commands.help, description="查看命令帮助")
        self.commands.register("stats", self._stats_command, description="查看当前会话的模型用量")
        self.extensions = prepared_extensions
        self.extensions.bind_runtime(
            base_tools=chosen_tools,
            base_dispatch=chosen_dispatch,
            permission_hook=chosen_permission,
            session=self.session,
            actions=self,
        )
        self.events.subscribe(SessionErrorRecorder(self.session))
        self.events.subscribe(JsonlTraceRecorder(self.trace_path))
        self.events.subscribe(self.usage)
        self.events.subscribe(self.extensions.handle_event)

        assistant_request = StreamingModelRequester(provider, timeout_seconds, self.events.emit)
        self.requester = assistant_request
        # s17 修改：请求链只使用已统一校验的压缩策略和摘要额度。
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
        # s17 修改：使用统一校验后的额度，循环及其计数语义保持 s16 行为。
        self.max_tokens = run_config.max_tokens
        self.max_rounds = run_config.max_rounds
        self._running = False
        self._closed = False
        # s18 新增：同步 handler 的触发意图由外层统一兑现，不重入 run。
        self._extension_callback_active = False
        self._turn_requested = False
        self._extensions_started = False

    # s16 修改：公开事件订阅的实际输入类型，继续复用 s08/s11 的分发规则。
    def subscribe(self, listener: EventSink) -> None:
        """把调用方观察者加入本实例的事件链。

        输入：listener(event)；输出：None，不返回取消订阅句柄。
        流程：交给 EventDispatcher 注册；运行时由 emit 按顺序通知，观察者失败则停止。
        """
        self.events.subscribe(listener)

    # s16 修改：补全可信注册的输入、输出和空闲时序说明，注册规则保持不变。
    def register_extension(self, register: Callable[[ExtensionAPI], None]) -> None:
        """在空闲实例上注册调用方选择的可信能力。

        输入：register(api)；输出：None，能力加入当前 ExtensionAPI。
        流程：检查关闭和运行标记 → 调用注册函数，函数可从会话恢复持久化工具状态。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或正在运行，不能更改能力")
        register(self.extensions)

    # s19 新增：参数定稿与状态恢复只执行一次，CLI 和 SDK 共用同一启动边界。
    def initialize_extensions(self) -> None:
        """固定参数并完成一次 session_start，兑现启动回调显式请求的回合。

        输入：已加载扩展与显式参数；输出：无。
        流程：校验参数 → 受控启动回调恢复状态或发送消息 → 按需运行同一个循环。
        边界：初始化中断关闭实例；已完成初始化后发生的模型错误按任务失败传播。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if self._extensions_started:
            return
        self.extensions.flags.validate()
        self.extensions.flags.frozen = True
        self._extensions_started = True
        self._turn_requested = False
        self._running = True
        self._extension_callback_active = True
        try:
            try:
                self.events.emit(
                    AgentEvent(
                        "session_start",
                        {
                            "session": self.session,
                            "session_path": str(self.session.path),
                        },
                    )
                )
            except BaseException:
                # s19 修复 B039：中断不能留下可继续使用的部分初始化实例。
                self._closed = True
                raise
            finally:
                self._extension_callback_active = False
            if self._turn_requested:
                # s19 新增：对齐 Pi 启动回调的消息能力，同步实现等初始化完成后再请求模型。
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
            self._running = False
            self._extension_callback_active = False
            self._turn_requested = False

    # s16 修改：用已有 Message 类型明确返回的是有效模型上下文，而非完整存储日志。
    def run(self, prompt: str) -> list[Message]:
        """保存任务、投影会话并运行；失败向调用方传播，不当作成功验收。

        输入：非空任务；输出：包含此前有效历史的本次模型上下文，不等于完整 JSONL 日志。
        流程：检查生命周期 → 追加用户消息 → 构建上下文 → 同一 loop → 释放运行状态。
        """
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或已有任务运行")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("任务不能为空")
        # s19 新增：第一项任务保存前完成参数校验和扩展状态恢复。
        self.initialize_extensions()
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
        # s19 新增：命令也必须看到已定稿的启动参数与恢复状态。
        self.initialize_extensions()
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
        # s19 新增：空闲时主动触发回合，也先完成一次扩展初始化。
        if trigger_turn and not self._extension_callback_active:
            self.initialize_extensions()
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

    # s16 修改：明确关闭入口与外部资源所有权，保留已有同步生命周期。
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
    extensions: ExtensionAPI | None = None,
    flag_values: Mapping[str, bool | str] | None = None,
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
        extensions=extensions,
        flag_values=flag_values,
    )
