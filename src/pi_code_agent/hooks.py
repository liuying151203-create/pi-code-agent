#!/usr/bin/env python3
"""s04：工具调用 Hooks。

本章复用 s03 的工具和权限策略，只把工具执行前后的扩展逻辑抽出来：

    tool_call -> before hooks -> dispatch -> after hooks -> tool_result

Hooks 让权限、日志和结果处理可以作为函数注入，而不必继续修改核心循环。
"""

from __future__ import annotations

# 来自 s03：保持；启动参数由入口解析，Hook 和核心循环不解析运行模式。
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pi_code_agent.permissions as previous

# ===== 来自 s03：工具、权限和消息类型（复用） =====
# s04 关注生命周期扩展，不重复实现 s03 已验证的工具处理和权限规则。

Message = previous.Message
DispatchTool = previous.DispatchTool
PermissionCheck = previous.PermissionCheck
PermissionConfirm = previous.PermissionConfirm
TOOLS = previous.TOOLS
TOOL_HANDLERS = previous.TOOL_HANDLERS
dispatch_tool = previous.dispatch_tool
WORKDIR = previous.WORKDIR

# ===== s04 修改：全局系统提示词 =====
# 相对 s03 继续精简提示词；Windows 命令语法由 bash 工具描述负责。

SYSTEM = (
    f"你是运行在 {WORKDIR} 的编程 Agent。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。"
)


# ===== s04 新增：Hook 类型和容器 =====

# s04 新增：前置 Hook 用返回值表达“继续”或“阻断”，不直接执行工具。
BeforeToolHook = Callable[[str, dict[str, Any]], str | None]
# s04 新增：后置 Hook 接收当前结果并返回处理后的结果，支持结果处理链。
AfterToolHook = Callable[[str, dict[str, Any], str], str]


@dataclass
class Hooks:
    """按工具调用生命周期保存前置和后置 Hook。

    属性：
        before_tool_call：在 dispatch 前按注册顺序执行，可返回字符串阻断调用。
        after_tool_call：在工具执行或阻断后按注册顺序执行，可返回新的结果文本。

    这是一个轻量的同步 Hook 容器。它不负责注册全局插件，也不负责并行调度，
    只为当前 Agent loop 提供清晰、可测试的生命周期依赖。
    """

    # s04 新增：前置 Hook 控制工具能否进入 dispatch。
    before_tool_call: list[BeforeToolHook] = field(default_factory=list)
    # s04 新增：后置 Hook 统一处理工具执行或阻断后的结果。
    after_tool_call: list[AfterToolHook] = field(default_factory=list)


# ===== s04 新增：Hook 执行器 =====


def run_before_hooks(
    name: str,
    arguments: dict[str, Any],
    hooks: Hooks,
) -> str | None:
    """按注册顺序运行前置 Hook，并返回第一个阻断原因。

    输入：工具名称、参数字典和 Hook 容器。
    输出：所有 Hook 放行时返回 `None`；某个 Hook 阻断时返回原因文本；Hook 自身
    抛出异常时返回可回传模型的错误文本。
    流程：依次调用 Hook → 遇到非空返回值立即停止 → 将阻断原因交给执行入口。
    """
    for hook in hooks.before_tool_call:
        try:
            # s04 新增：前置 Hook 返回字符串表示阻断，None 表示继续执行。
            block_reason = hook(name, arguments)
        except Exception as error:  # noqa: BLE001 - Hook 错误要转成工具结果
            return f"Hook error: {error}"
        if block_reason is not None:
            return block_reason
    return None


def run_after_hooks(
    name: str,
    arguments: dict[str, Any],
    result: str,
    hooks: Hooks,
) -> str:
    """按注册顺序处理工具结果，并把结果传给下一个 Hook。

    输入：工具名称、参数字典、当前结果文本和 Hook 容器。
    输出：经过全部后置 Hook 处理后的结果文本。
    流程：第一个 Hook 接收原始结果，后续 Hook 接收前一个 Hook 的返回值；如果
    某个 Hook 出错，则保留当前结果并追加错误说明，避免隐藏工具结果。
    """
    current = result
    for hook in hooks.after_tool_call:
        try:
            # s04 新增：后置 Hook 接收当前结果，并把处理后的结果传给下一个 Hook。
            current = hook(name, arguments, current)
        except Exception as error:  # noqa: BLE001 - 保留结果并报告 Hook 错误
            current = f"{current}\nHook error: {error}"
    return current


# ===== s04 修改：把 s03 权限策略接入 before hook =====
# 权限仍由 s03 的规则决定；s04 只改变它进入工具生命周期的方式。


def make_permission_hook(
    permission: PermissionCheck = previous.check_permission,
    confirm: PermissionConfirm = previous.confirm_permission,
) -> BeforeToolHook:
    """创建一个复用 s03 权限策略的前置 Hook。

    输入：可替换的权限检查函数和用户确认函数；默认使用 s03 的实现。
    输出：符合 `BeforeToolHook` 签名的闭包，允许时返回 `None`，阻断时返回原因文本。
    流程：调用权限检查 → 拒绝则阻断 → 询问状态请求用户确认 → 确认后放行。
    这样 s04 只改变权限逻辑的接入位置，不复制或重写 s03 的规则。
    """

    def permission_hook(name: str, arguments: dict[str, Any]) -> str | None:
        decision = permission(name, arguments)
        if decision.status is previous.PermissionStatus.DENY:
            return f"Permission denied: {decision.reason}"
        if decision.status is previous.PermissionStatus.ASK and not confirm(
            name, arguments, decision.reason
        ):
            return "Permission denied: user did not approve this tool call"
        return None

    return permission_hook


# ===== s04 修改：工具执行入口 =====


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    dispatch: DispatchTool,
    hooks: Hooks,
) -> str:
    """运行一次完整的工具调用生命周期。

    作用：先执行所有 `before_tool_call`，未被阻断时调用工具，再执行所有
    `after_tool_call`。前置 Hook 可以阻止工具，后置 Hook 可以观察或转换结果。

    输入：工具名称、工具参数、实际分发函数和 Hook 容器。
    输出：经过前后置 Hook 处理的工具结果文本。
    流程：前置 Hook → 工具分发或阻断 → 后置 Hook；任一前置 Hook 返回阻断原因后，
    后续前置 Hook 和工具处理函数都不会执行，但后置 Hook 仍能看到最终结果。
    """
    # s04 修改：相对 s03，工具执行前改为统一运行 before hooks。
    block_reason = run_before_hooks(name, arguments, hooks)
    if block_reason is None:
        result = dispatch(name, arguments)
    else:
        result = block_reason
    # s04 新增：无论执行还是阻断，都交给 after hooks 做统一收尾。
    return run_after_hooks(name, arguments, result, hooks)


# ===== 来自 s03：响应读取辅助（保持） =====


def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ===== s04 修改：核心循环注入 Hooks =====
# s03 注入 permission/confirm；s04 将这两个依赖组合进 Hooks，循环只关心生命周期。


# ===== 来自 s03：终端输出和交互入口；s04 修改 Hook 配置 =====


def _text_from_content(content: Any) -> str:
    """提取模型响应中的文本块，用于终端展示。"""
    if isinstance(content, str):
        return content
    return "\n".join(
        str(text)
        for block in content
        if _get(block, "type") == "text"
        if (text := _get(block, "text"))
    )
