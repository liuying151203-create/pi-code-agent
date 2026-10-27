# ===== s11 快照 =====
# 来自 s10：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s02：工具声明、完整参数校验及异步执行边界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .messages import ToolCall, ToolResultMessage
from .model import ToolDefinition


# 来自 s02：保持；Pydantic 同时生成 JSON Schema 和校验实参，避免手写不完整的 schema 解释器。
class NoArguments(BaseModel):
    """声明无参数工具；模型额外提供字段时拒绝执行。"""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolResult:
    """保存工具正文与错误状态；正文以 Error 开头也不自动判定失败。"""

    content: str
    is_error: bool = False
    # 来自 s08：保持；整批工具都要求终止时停止自动模型续轮，不写入消息协议。
    terminate: bool = False

    def __post_init__(self) -> None:
        """拒绝无法可靠投影到模型协议的结果。"""
        if (
            not isinstance(self.content, str)
            or type(self.is_error) is not bool
            or type(self.terminate) is not bool
        ):
            raise TypeError("工具结果必须包含文本 content 和布尔 is_error")


@dataclass(frozen=True)
class Tool:
    """把工具的模型定义和实际执行函数绑定在一起。

    输入：名称、说明、异步处理函数和参数模型；输出：可注册工具。
    流程：definition 生成 schema → execute_tool 校验实参 → 调用 execute。
    边界：参数校验不提供 OS 隔离；当前工具只返回文本，不预留未实现的生命周期方法。
    """

    name: str
    description: str
    execute: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    parameters: type[BaseModel] = NoArguments

    def __post_init__(self) -> None:
        """提前检查声明，避免请求模型后才发现工具不可执行。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("工具名称不能为空")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("工具说明不能为空")
        if not callable(self.execute):
            raise TypeError("工具 execute 必须可调用")
        if not isinstance(self.parameters, type) or not issubclass(self.parameters, BaseModel):
            raise TypeError("parameters 必须是 Pydantic 参数模型")

    def definition(self) -> ToolDefinition:
        """生成本次请求的独立定义，模型适配器改写 schema 时不影响原声明。"""
        return ToolDefinition(self.name, self.description, self.parameters.model_json_schema())


# 来自 s02：保持；参考 Pi validateToolArguments，校验副本并保留类型转换，不修改原始调用。
async def execute_tool(call: ToolCall, tools: Mapping[str, Tool]) -> ToolResultMessage:
    """兼容已有 SDK：执行工具并只返回模型消息。"""
    result = await execute_tool_outcome(call, tools)
    return ToolResultMessage(call.id, call.name, result.content, result.is_error)


# 来自 s08：保持；结果策略与模型消息分开，保留 Pi 的 terminate 批处理语义。
async def execute_tool_outcome(
    call: ToolCall, tools: Mapping[str, Tool], *, before=None, after=None
) -> ToolResult:
    """校验并执行一次工具，普通失败以对应 ID 的错误结果返回。

    输入：模型工具调用和本次工具表；输出：独立工具结果消息。
    流程：查找名称 → 复制/规范化参数 → Pydantic 校验 → await execute → 包装结果。
    边界：CancelledError 直接传播；不把取消当普通工具失败。
    """
    try:
        tool = tools.get(call.name)
        if tool is None:
            raise ValueError(f"工具不存在：{call.name}")
        if not isinstance(call.arguments, dict):
            raise TypeError("工具参数必须是 JSON 对象")
        arguments = deepcopy(call.arguments)
        # 来自 s02：保持；对齐 Pi 对非必填、非 nullable 字段的 null 规范化。
        for name, field in tool.parameters.model_fields.items():
            if arguments.get(name, ...) is None and not field.is_required():
                try:
                    TypeAdapter(field.annotation).validate_python(None)
                except ValidationError:
                    arguments.pop(name)
        validated = tool.parameters.model_validate(arguments).model_dump()
        # 来自 s08：保持；Hook 只能在完整参数校验之后介入；异常不会执行工具。
        decision = await before(call, deepcopy(validated)) if before is not None else None
        if decision and decision.get("block"):
            result = ToolResult(
                decision.get("reason", "工具被拦截"), True, decision.get("terminate", False)
            )
        else:
            result = await tool.execute(validated)
        if not isinstance(result, ToolResult):
            raise TypeError("工具处理函数必须返回 ToolResult")
    except Exception as error:  # noqa: BLE001 - Pi 的普通工具异常回传模型；不吞取消异常
        result = ToolResult(str(error), is_error=True)
    # 来自 s08：保持；成功、执行失败和校验失败均可观察/覆盖结果，取消不会伪装成结果。
    if after is not None:
        try:
            result = await after(call, result)
            if not isinstance(result, ToolResult):
                raise TypeError("after 必须返回 ToolResult")
        except Exception as error:  # noqa: BLE001 - 策略失败回传错误结果，不吞取消
            result = ToolResult(str(error), True)
    return result
