"""s15 新增：区分给模型看的工具正文与给会话保存的状态元数据。"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pi_code_agent.events import EventDispatchError
from pi_code_agent.hooks import Hooks, run_after_hooks, run_before_hooks
from pi_code_agent.sandbox_copy.sandbox import SandboxError
from pi_code_agent.session import SessionPersistenceError


# s15 新增：参考 Pi 的 content/details 分工，不把扩展状态混进模型协议字段。
@dataclass(frozen=True)
class ToolResult:
    """保存一次工具执行的正文和可选状态快照。

    输入：字符串正文、可选的 JSON 对象 details。
    输出：供循环分别展示、发送给模型及持久化的结果对象。
    流程：构造时检查类型并复制 details → 循环保存正文与元数据 → 事件观察者读取快照。
    边界：本对象不保存会话、不执行工具；复制快照避免同批后续操作覆盖较早结果。
    """

    content: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """检查元数据可序列化，并隔离工具内部的可变状态。"""
        if not isinstance(self.content, str):
            raise TypeError("工具结果 content 必须是字符串")
        if self.details is not None:
            if not isinstance(self.details, dict):
                raise TypeError("工具结果 details 必须是对象或 None")
            json.dumps(self.details, ensure_ascii=False, allow_nan=False)
            object.__setattr__(self, "details", deepcopy(self.details))


# s15 修改：相对 s14，分发函数允许返回带状态的结果；原有字符串工具仍可用。
type DispatchTool = Callable[[str, dict[str, Any]], str | ToolResult]


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    dispatch: DispatchTool,
    hooks: Hooks,
) -> ToolResult:
    """运行已有 Hook 生命周期，同时保留结构化工具元数据。

    输入：模型工具名、参数、分发函数及 s04 Hooks；输出：统一的 ToolResult。
    流程：前置 Hook → 分发或阻断 → 包装字符串 → 后置 Hook 处理正文 → 返回结果。
    边界：Hook 仍接收字符串，details 不参与文本转换；后端致命异常交给核心循环处理。
    """
    # 来自 s04：保持；前置 Hook 在分发之前执行，拒绝时不调用 handler。
    reason = run_before_hooks(name, arguments, hooks)
    if reason is not None:
        raw_result = reason
    else:
        # s01 修改：普通工具参数/文件错误回传模型，致命后端和观察链异常仍由外层停止。
        try:
            raw_result = dispatch(name, arguments)
        except (EventDispatchError, SessionPersistenceError, SandboxError):
            raise
        except Exception as error:  # noqa: BLE001 - 对齐 Pi：普通 handler 失败属于工具结果
            raw_result = f"Error: {error}"
    # s15 新增：旧工具字符串转为无元数据结果，扩展返回的快照保持独立。
    if isinstance(raw_result, ToolResult):
        result = raw_result
    else:
        result = ToolResult(content=raw_result)
    # s15 修改：后置 Hook 只修改给模型看的正文，不能丢掉会话状态。
    content = run_after_hooks(name, arguments, result.content, hooks)
    return ToolResult(content=content, details=result.details)
