"""外部应用使用的同步 SDK：显式组装组件，不复制核心循环。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.config import RunConfig, validate_timeout
from pi_code_agent.events import (
    EventDispatcher,
    EventSink,
    JsonlTraceRecorder,
    UsageTracker,
    new_trace_path,
)
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
from pi_code_agent.serialization import Message, select_session_path
from pi_code_agent.session import SessionManager

# s16 新增：构造器与工厂共用现有提示词默认值，明确公开参数时避免两处漂移。
DEFAULT_SYSTEM = "你是编程 Agent。仅在任务需要时使用工具，优先专用工具，完成后停止。"


# s16 修改：明确组装层的公开输出与职责，具体运行机制继续复用已有组件。
class Agent:
    """将一份会话的模型、工具、存储与观察链组装成独立实例。

    输入：Provider、工作区，以及可替换的后端、权限、存储和压缩配置。
    输出：run 返回本次运行的模型上下文；subscribe 观察事件；register_extension 接入能力。
    流程：绑定会话 → 创建事件链 → 组装请求器 → 注册能力 → 保存任务并执行核心循环。
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
