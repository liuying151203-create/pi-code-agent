# ===== 来自 s03：保持 =====
# 来自 s03：保持；保存本章阶段的组件实现；相对导入只引用本章，不读取正式包或其他章节。
"""s03：将统一文本/工具消息转换为 Anthropic 或 OpenAI Chat 协议。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Self

from .config import ModelConfig
from .messages import (
    AssistantMessage,
    Message,
    Text,
    Thinking,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from .model import ToolDefinition


class ProviderError(RuntimeError):
    """表示模型依赖加载、网络请求或响应转换失败，不包装为工具执行错误。"""


# 来自 s03：保持；对应 Pi normalizeToolCallId，同时用于请求和结果，保持关联不变。
def _anthropic_id(value: str) -> str:
    """限制为 Anthropic 接受的字符和长度，不改写内部历史标识。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:64]


def _anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """在适配边界把独立工具结果转换为连续的 user/tool_result 块。"""
    result = []
    for message in messages:
        # 来自 s03：保持；失败回答只留在本地，不重放未完成调用。
        if isinstance(message, AssistantMessage) and message.stop_reason in {"error", "aborted"}:
            continue
        if isinstance(message, UserMessage):
            result.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            blocks = []
            for block in message.content:
                if isinstance(block, Text):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, Thinking):
                    # s03 修改：按 Pi 原样回传签名和脱敏数据，不能把推理当普通回答丢弃。
                    if block.redacted:
                        blocks.append({"type": "redacted_thinking", "data": block.signature})
                    elif block.signature.strip():
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": block.text,
                                "signature": block.signature,
                            }
                        )
                    elif block.text.strip():
                        # s03 修改：沿用 Pi 对无签名块的文本回退，只改变请求投影，不改变本地类型。
                        blocks.append({"type": "text", "text": block.text})
                else:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": _anthropic_id(block.id),
                            "name": block.name,
                            "input": deepcopy(block.arguments)
                            if isinstance(block.arguments, dict)
                            else {},
                        }
                    )
            # 来自 s03：保持；没有可投影内容时不发送空模型消息。
            if blocks:
                result.append({"role": "assistant", "content": blocks})
        else:
            block = {
                "type": "tool_result",
                "tool_use_id": _anthropic_id(message.tool_call_id),
                "content": message.content,
                "is_error": message.is_error,
            }
            if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
    return result


def _openai_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    """把内部消息转换为 system/user/assistant/tool，保留调用 ID 和错误反馈。"""
    result = [{"role": "system", "content": system}] if system else []
    for message in messages:
        # 来自 s03：保持；失败回答只留在本地，不重放未完成调用。
        if isinstance(message, AssistantMessage) and message.stop_reason in {"error", "aborted"}:
            continue
        if isinstance(message, UserMessage):
            result.append({"role": "user", "content": message.content})
        elif isinstance(message, ToolResultMessage):
            # 来自 s03：保持；对齐 Pi，Chat 没有 is_error 字段时原样传正文，内部错误标记保留。
            content = message.content
            result.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": content}
            )
        else:
            content = "\n".join(block.text for block in message.content if isinstance(block, Text))
            converted = {"role": "assistant", "content": content or None}
            calls = [block for block in message.content if isinstance(block, ToolCall)]
            # 来自 s03：保持；Chat 不重放 Anthropic 推理签名，也不产生空回答。
            if not content and not calls:
                continue
            if calls:
                converted["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments
                            if isinstance(call.arguments, str)
                            else json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            result.append(converted)
    return result


# 来自 s03：保持；客户端拥有者负责关闭；注入的客户端由外部调用方管理。
class AnthropicModel:
    """使用 Anthropic Messages，把完整响应转换成统一回答。

    输入：显式配置和可选客户端；输出：complete 返回 AssistantMessage。
    流程：构造真实客户端 → 转换请求 → await API → 归一化停止状态及用量。
    边界：保留文本、推理及签名与客户端工具，不处理图片或服务端工具。
    """

    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        """接收配置和可选客户端，校验协议后创建或借用客户端，记录关闭责任。"""
        if config.provider != "anthropic":
            raise ValueError("AnthropicModel 需要 anthropic 配置")
        self.config = config
        self._owns_client = client is None
        # 来自 s03：保持；仅创建自有客户端时导入对应 SDK，注入与离线模式不触发导入。
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ModuleNotFoundError as error:
                raise ProviderError(
                    f"当前 Python 环境缺少模型依赖 {error.name}，请按项目 README 配置依赖"
                ) from error
            self.client = AsyncAnthropic(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        else:
            self.client = client

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AssistantMessage:
        """发送一次请求并验证响应；请求失败向调用方抛 ProviderError。"""
        request = {
            "model": self.config.model,
            "messages": _anthropic_messages(messages),
            "max_tokens": max_tokens,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": deepcopy(tool.parameters),
                }
                for tool in tools
            ]
        try:
            raw = await self.client.messages.create(**request)
            content = []
            for block in raw.content:
                if block.type == "text":
                    content.append(Text(block.text))
                # 来自 s03：保持；保留推理签名与脱敏数据，避免后续请求丢失信息。
                elif block.type == "thinking":
                    # s03 修改：空推理文本也可能有有效签名，两者分开保存，终端仍只显示 Text。
                    content.append(
                        Thinking(
                            getattr(block, "thinking", "") or "",
                            getattr(block, "signature", "") or "",
                        )
                    )
                elif block.type == "redacted_thinking":
                    content.append(Thinking("", block.data, redacted=True))
                elif block.type == "tool_use":
                    content.append(ToolCall(block.id, block.name, block.input))
                else:
                    raise ProviderError(f"当前不支持 Anthropic 内容块：{block.type}")
            reasons = {
                "end_turn": "stop",
                "stop_sequence": "stop",
                "tool_use": "tool_use",
                "max_tokens": "length",
                "pause_turn": "stop",
                "refusal": "error",
                "sensitive": "error",
            }
            if raw.stop_reason not in reasons:
                raise ProviderError(f"当前不支持 Anthropic 停止状态：{raw.stop_reason}")
            usage = (
                Usage(raw.usage.input_tokens, raw.usage.output_tokens)
                if raw.usage is not None
                else None
            )
            reason = reasons[raw.stop_reason]
            explanation = f"Provider stop_reason: {raw.stop_reason}" if reason == "error" else None
            return AssistantMessage(tuple(content), reason, usage, explanation)
        except Exception as error:
            raise ProviderError(f"Anthropic 请求或响应失败：{error}") from error

    async def aclose(self) -> None:
        """仅关闭本实例创建的客户端，允许重复关闭。"""
        if self._owns_client:
            await self.client.close()
            self._owns_client = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


class OpenAIModel:
    """适配 OpenAI Chat Completions 兼容服务，核心循环不接触其协议字段。

    输入：显式兼容接口配置和可选客户端；输出：归一化回答。
    流程：转换消息/工具 → await API → 解析函数参数 → 转换停止状态和用量。
    边界：额度字段显式配置，不根据模型名猜测兼容性；这里只支持 function 工具。
    """

    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        """接收配置和可选客户端，校验协议后创建或借用客户端，记录关闭责任。"""
        if config.provider != "openai-compatible":
            raise ValueError("OpenAIModel 需要 openai-compatible 配置")
        self.config = config
        self._owns_client = client is None
        # s03 修改：OpenAI SDK 在对应客户端真正被选中时才加载，不影响 Anthropic。
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ModuleNotFoundError as error:
                raise ProviderError(
                    f"当前 Python 环境缺少模型依赖 {error.name}，请按项目 README 配置依赖"
                ) from error
            self.client = AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        else:
            self.client = client

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AssistantMessage:
        """解析函数调用的 JSON；不合法的参数交回工具校验，不能提前执行。"""
        request = {
            "model": self.config.model,
            "messages": _openai_messages(system, messages),
            self.config.token_parameter: max_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": deepcopy(tool.parameters),
                    },
                }
                for tool in tools
            ]
        try:
            raw = await self.client.chat.completions.create(**request)
            if not raw.choices:
                raise ProviderError("服务返回空 choices")
            choice = raw.choices[0]
            message = choice.message
            content = [Text(message.content)] if message.content else []
            for call in message.tool_calls or []:
                if call.type != "function":
                    raise ProviderError(f"当前不支持 OpenAI 工具类型：{call.type}")
                try:
                    arguments = json.loads(call.function.arguments)
                except (ValueError, TypeError):
                    arguments = call.function.arguments
                content.append(ToolCall(call.id, call.function.name, arguments))
            reasons = {
                "stop": "stop",
                "end": "stop",
                None: "stop",
                "tool_calls": "tool_use",
                "length": "length",
                "content_filter": "error",
            }
            refusal = getattr(message, "refusal", None)
            usage = (
                Usage(raw.usage.prompt_tokens, raw.usage.completion_tokens)
                if raw.usage is not None
                else None
            )
            # 来自 s03：保持；对齐 Pi，未知完成状态也记录为模型失败，不执行其中的工具。
            reason = "error" if refusal else reasons.get(choice.finish_reason, "error")
            explanation = (
                (refusal or f"Provider finish_reason: {choice.finish_reason}")
                if reason == "error"
                else None
            )
            return AssistantMessage(tuple(content), reason, usage, explanation)
        except Exception as error:
            raise ProviderError(f"OpenAI 兼容请求或响应失败：{error}") from error

    async def aclose(self) -> None:
        """仅关闭本实例创建的客户端。"""
        if self._owns_client:
            await self.client.close()
            self._owns_client = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


def create_model(config: ModelConfig) -> AnthropicModel | OpenAIModel:
    """按已校验协议创建模型，SDK 不隐式加载环境或改写调用方配置。"""
    if config.provider == "anthropic":
        return AnthropicModel(config)
    return OpenAIModel(config)
