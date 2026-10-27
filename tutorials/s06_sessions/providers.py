# ===== 来自 s05：保持 =====
# 来自 s05：保持；本章组件只相对导入本章，冻结当时运行行为。
"""s03：将统一文本/工具消息转换为 Anthropic 或 OpenAI Chat 协议。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Sequence
from copy import deepcopy
from typing import Any, Self

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from .config import ModelConfig
from .messages import (
    AssistantMessage,
    Message,
    Text,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from .model import AssistantStreamEvent, ToolDefinition


class ProviderError(RuntimeError):
    """表示网络请求或供应商响应无法完成当前统一协议，不包装为工具错误。"""

    # 来自 s05：保持；异常可携带最新快照，包含尚未触发文本事件的已收到用量。
    def __init__(self, message: str, *, partial: AssistantMessage | None = None) -> None:
        super().__init__(message)
        self.partial = partial


# 来自 s03：保持；对应 Pi normalizeToolCallId，同时用于请求和结果，保持关联不变。
def _anthropic_id(value: str) -> str:
    """限制为 Anthropic 接受的字符和长度，不改写内部历史标识。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:64]


def _anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """在适配边界把独立工具结果转换为连续的 user/tool_result 块。"""
    result = []
    for message in messages:
        # 来自 s05：保持；对齐 Pi transformMessages，失败快照保留在本地，不重放给供应商。
        if isinstance(message, AssistantMessage):
            if message.stop_reason == "pending":
                raise ProviderError("不能把 pending 快照作为新请求的历史")
            if message.stop_reason in {"error", "aborted"}:
                continue
        if isinstance(message, UserMessage):
            result.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            blocks = []
            for block in message.content:
                if isinstance(block, Text):
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
        # 来自 s05：保持；对齐 Pi transformMessages，失败快照保留在本地，不重放给供应商。
        if isinstance(message, AssistantMessage):
            if message.stop_reason == "pending":
                raise ProviderError("不能把 pending 快照作为新请求的历史")
            if message.stop_reason in {"error", "aborted"}:
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


# 来自 s05：保持；同一投影处理完整响应和 SDK 当前快照，不在核心循环解释供应商块。
def _anthropic_response(raw: Any, *, partial: bool = False) -> AssistantMessage:
    """转换供应商回答/快照；partial 保留 pending 状态，最终状态仍须验证。"""
    content = []
    for block in raw.content:
        if block.type == "text":
            content.append(Text(block.text))
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
    if not partial and raw.stop_reason not in reasons:
        raise ProviderError(f"当前不支持 Anthropic 停止状态：{raw.stop_reason}")
    usage = (
        Usage(raw.usage.input_tokens, raw.usage.output_tokens) if raw.usage is not None else None
    )
    reason = "pending" if partial else reasons[raw.stop_reason]
    explanation = f"Provider stop_reason: {raw.stop_reason}" if reason == "error" else None
    return AssistantMessage(tuple(content), reason, usage, explanation)


# 来自 s03：保持；客户端拥有者负责关闭；注入的客户端由外部调用方管理。
class AnthropicModel:
    """使用 Anthropic Messages，把完整响应转换成统一回答。

    输入：显式配置和可选客户端；输出：complete 返回 AssistantMessage。
    流程：构造真实客户端 → 转换请求 → await API → 归一化停止状态及用量。
    边界：只处理文本与客户端工具，不假装支持图片、服务端工具或思考块。
    """

    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        if config.provider != "anthropic":
            raise ValueError("AnthropicModel 需要 anthropic 配置")
        self.config = config
        self._owns_client = client is None
        self.client = (
            client
            if client is not None
            else AsyncAnthropic(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        )

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
            return _anthropic_response(raw)
        except Exception as error:
            raise ProviderError(f"Anthropic 请求或响应失败：{error}") from error

    # 来自 s05：保持；直接消费 SDK 的网络流，上下文负责提前退出时关闭连接。
    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AsyncGenerator[AssistantStreamEvent, None]:
        """逐段返回文本快照，工具 JSON 组装完成后才交给最终回答，异常继续传播。"""
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
        partial = AssistantMessage((), "pending")
        try:
            async with self.client.messages.stream(**request) as raw_stream:
                yield {"type": "start", "partial": AssistantMessage((), "pending")}
                tool_json: dict[int, str] = {}
                stopped = False
                async for event in raw_stream:
                    # 来自 s05：保持；收到元数据就更新快照，即使文字还没到也能保留用量。
                    if event.type.startswith(("message_", "content_block_")):
                        partial = _anthropic_response(
                            raw_stream.current_message_snapshot, partial=True
                        )
                    if event.type == "content_block_start":
                        if event.content_block.type == "text":
                            partial = _anthropic_response(
                                raw_stream.current_message_snapshot, partial=True
                            )
                            yield {
                                "type": "text_start",
                                "content_index": event.index,
                                "partial": partial,
                            }
                            if event.content_block.text:
                                yield {
                                    "type": "text_delta",
                                    "content_index": event.index,
                                    "delta": event.content_block.text,
                                    "partial": partial,
                                }
                        elif event.content_block.type != "tool_use":
                            raise ProviderError(
                                f"当前不支持 Anthropic 内容块：{event.content_block.type}"
                            )
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            partial = _anthropic_response(
                                raw_stream.current_message_snapshot, partial=True
                            )
                            yield {
                                "type": "text_delta",
                                "content_index": event.index,
                                "delta": event.delta.text,
                                "partial": partial,
                            }
                        elif event.delta.type == "input_json_delta":
                            tool_json[event.index] = (
                                tool_json.get(event.index, "") + event.delta.partial_json
                            )
                        else:
                            raise ProviderError(f"当前不支持 Anthropic 增量：{event.delta.type}")
                    elif event.type == "content_block_stop":
                        snapshot = raw_stream.current_message_snapshot
                        block = snapshot.content[event.index]
                        if block.type == "text":
                            yield {
                                "type": "text_end",
                                "content_index": event.index,
                                "content": block.text,
                                "partial": _anthropic_response(snapshot, partial=True),
                            }
                    elif event.type == "message_stop":
                        stopped = True
                if not stopped:
                    raise ProviderError("Anthropic 流缺少 message_stop，回答未完成")
                final = _anthropic_response(await raw_stream.get_final_message())
                content = list(final.content)
                # s05 调整：Pi 可展示宽松 JSON 快照；本章重新解析原始 JSON，解析失败交回参数校验。
                for index, encoded in tool_json.items():
                    call = content[index]
                    if not isinstance(call, ToolCall):
                        raise ProviderError("工具 JSON 片段没有对应工具调用")
                    try:
                        arguments = json.loads(encoded)
                    except ValueError:
                        arguments = encoded
                    content[index] = ToolCall(call.id, call.name, arguments)
                yield {
                    "type": "done",
                    "message": AssistantMessage(
                        tuple(content), final.stop_reason, final.usage, final.error_message
                    ),
                }
        except asyncio.CancelledError as error:
            error.partial = partial
            raise
        except Exception as error:
            raise ProviderError(
                f"Anthropic 流式请求或响应失败：{error}", partial=partial
            ) from error

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
        if config.provider != "openai-compatible":
            raise ValueError("OpenAIModel 需要 openai-compatible 配置")
        self.config = config
        self._owns_client = client is None
        self.client = (
            client
            if client is not None
            else AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        )

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

    # 来自 s05：保持；Chat 每块使用 delta；按工具 index 拼接参数，完整 JSON 到齐前不执行。
    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AsyncGenerator[AssistantStreamEvent, None]:
        """打开 Chat 网络流，保留尾部用量，返回文本增量及最终工具调用。"""
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
        request["stream"] = True
        if self.config.stream_include_usage:
            request["stream_options"] = {"include_usage": True}
        text, text_started, usage = "", False, None
        try:
            raw_stream = await self.client.chat.completions.create(**request)
            async with raw_stream:
                yield {"type": "start", "partial": AssistantMessage((), "pending")}
                calls: dict[int, dict[str, str]] = {}
                finish_reason = None
                refusal = ""
                async for chunk in raw_stream:
                    if chunk.usage is not None:
                        usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
                    # 来自 s05：保持；最后的 usage 块可没有 choices，不能提前结束迭代。
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta.content:
                        if not text_started:
                            text_started = True
                            yield {
                                "type": "text_start",
                                "content_index": 0,
                                "partial": AssistantMessage((Text(""),), "pending", usage),
                            }
                        text += delta.content
                        yield {
                            "type": "text_delta",
                            "content_index": 0,
                            "delta": delta.content,
                            "partial": AssistantMessage((Text(text),), "pending", usage),
                        }
                    refusal += getattr(delta, "refusal", None) or ""
                    for call in delta.tool_calls or []:
                        if call.type not in {None, "function"}:
                            raise ProviderError("当前只支持 function 工具增量")
                        if type(call.index) is not int or call.index < 0:
                            raise ProviderError("工具增量缺少有效 index")
                        current = calls.setdefault(
                            call.index, {"id": "", "name": "", "arguments": ""}
                        )
                        # 来自 s05：保持；对齐 Pi，id/name 采用第一次非空值，参数才是连续 JSON 片段。
                        current["id"] = current["id"] or call.id or ""
                        if call.function is not None:
                            current["name"] = current["name"] or call.function.name or ""
                            current["arguments"] += call.function.arguments or ""
                if finish_reason is None:
                    raise ProviderError("OpenAI 流缺少 finish_reason，回答未完成")
                if text_started:
                    yield {
                        "type": "text_end",
                        "content_index": 0,
                        "content": text,
                        "partial": AssistantMessage((Text(text),), "pending", usage),
                    }
                content = [Text(text)] if text_started else []
                for index in sorted(calls):
                    call = calls[index]
                    try:
                        arguments = json.loads(call["arguments"])
                    except ValueError:
                        arguments = call["arguments"]
                    content.append(ToolCall(call["id"], call["name"], arguments))
                reasons = {
                    "stop": "stop",
                    "end": "stop",
                    "tool_calls": "tool_use",
                    "length": "length",
                }
                reason = "error" if refusal else reasons.get(finish_reason, "error")
                explanation = (
                    (refusal or f"Provider finish_reason: {finish_reason}")
                    if reason == "error"
                    else None
                )
                yield {
                    "type": "done",
                    "message": AssistantMessage(tuple(content), reason, usage, explanation),
                }
        except asyncio.CancelledError as error:
            error.partial = AssistantMessage(
                (Text(text),) if text_started else (), "pending", usage
            )
            raise
        except Exception as error:
            raise ProviderError(
                f"OpenAI 兼容流式请求或响应失败：{error}",
                partial=AssistantMessage((Text(text),) if text_started else (), "pending", usage),
            ) from error

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
