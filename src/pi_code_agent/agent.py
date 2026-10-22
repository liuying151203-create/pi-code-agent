"""外部应用使用的同步 SDK：显式组装组件，不复制核心循环。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.events import EventDispatcher, JsonlTraceRecorder, UsageTracker, new_trace_path
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
from pi_code_agent.serialization import select_session_path
from pi_code_agent.session import SessionManager


class Agent:
    """将一份会话的模型、工具、存储与观察链组装成独立实例。

    输入：Provider、工作区，以及可替换的后端、权限、存储和压缩配置。
    输出：run 返回完整消息；subscribe 观察事件；register_extension 接入能力。
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
        dispatch: Callable | None = None,
        permission_hook: Callable | None = None,
        compaction_policy: CompactionPolicy | None = None,
        summary_max_tokens: int = 4096,
        timeout_seconds: float = 60,
        max_tokens: int = 8000,
        max_rounds: int | None = None,
        system: str = "你是编程 Agent。仅在任务需要时使用工具，优先专用工具，完成后停止。",
    ) -> None:
        """按基础依赖、组件、运行时组装；构造期间不请求模型。

        自定义后端须同时提供 tools/dispatch/permission_hook，避免只换工具目录却遗漏门禁。
        data_dir 是显式宿主记录目录；外部产品不必写入底座安装位置。
        """
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
        self._running = False
        self._closed = False

    def subscribe(self, listener: Callable) -> None:
        """订阅已有运行事件；观察者失败沿用明确停止策略。"""
        self.events.subscribe(listener)

    def register_extension(self, register: Callable[[ExtensionAPI], None]) -> None:
        """空闲时注册可信扩展，注册函数可以读取持久化工具状态。"""
        if self._closed or self._running:
            raise RuntimeError("Agent 已关闭或正在运行，不能更改能力")
        register(self.extensions)

    def run(self, prompt: str) -> list[dict[str, Any]]:
        """保存任务、投影会话并运行；失败向调用方传播，不当作成功验收。

        输入：非空任务；输出：完整消息。
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

    def close(self) -> None:
        """关闭空闲实例；外部后端由其创建者关闭，不伪造并发取消能力。"""
        if self._running:
            raise RuntimeError("同步任务尚未结束，不能关闭")
        self._closed = True


def create_agent(**kwargs: Any) -> Agent:
    """创建同步 Agent；参数与 Agent 相同，不自动配置环境或扫描插件。"""
    return Agent(**kwargs)
