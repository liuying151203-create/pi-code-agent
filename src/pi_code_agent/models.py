"""来自 s10：保持 Provider 协议、SDK 适配和统一响应。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic import Anthropic

from pi_code_agent import serialization as s05

# s17 新增：模型参数由配置边界解析和校验，协议适配器继续保持原有职责。
from pi_code_agent.config import ModelConfig, model_config_from_env

Message = s05.Message


@dataclass(frozen=True)
class ModelUsage:
    """保存一次模型响应中与 Provider 无关的 Token 用量。

    作用：屏蔽 Anthropic 和 OpenAI-compatible usage 字段名称差异。
    输入：输入、输出、缓存读取和缓存写入 Token 数。
    输出：Provider 统一返回该对象；`total_tokens` 给出总用量。
    流程：Provider 读取 SDK 响应 → 构造 ModelUsage → 放入 ModelResponse。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """返回输入、输出和缓存 Token 的总和。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def to_dict(self) -> dict[str, int]:
        """转成可写入事件或 JSON 的普通字典。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
        }


class ModelResponseError(RuntimeError):
    """报告响应未完成或转换失败，并保留已经收到的 Token 用量。

    输入：失败说明和 Provider 已获得的 usage；输出：供请求器捕获的异常。
    流程：Provider 检查响应 → 失败时抛出 → s11 通过原有失败事件保存用量。
    边界：没有收到 usage 时仍为未知，不重新请求模型，也不返回可执行 content。
    """

    def __init__(self, message: str, usage: ModelUsage) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class ModelResponse:
    """描述一次已经归一化的模型响应。

    作用：向 Agent loop 提供统一内容块、模型信息、停止原因和 Token 用量。
    输入：Provider 从原始 SDK 响应转换得到的数据。
    输出：`content` 使用项目既有 `text` / `tool_use` 块，可直接保存和执行。
    流程：SDK 响应 → Provider 转换 → 请求器返回 → agent_loop 读取 content。
    """

    content: list[dict[str, Any]]
    model: str
    stop_reason: str | None
    usage: ModelUsage


class ModelProvider(Protocol):
    """定义 Agent 需要的阻塞与流式模型能力。"""

    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送阻塞请求并返回统一响应。"""
        ...

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """发送流式请求，报告文本片段并返回统一最终响应。"""
        ...


@dataclass(frozen=True)
class AnthropicProvider:
    """把 Anthropic Messages API 适配为统一 ModelProvider。

    作用：保留 s09 的 Anthropic 请求方式，并把 SDK 响应转换为 `ModelResponse`。
    输入：Anthropic 客户端、模型名称和统一请求参数。
    输出：阻塞或流式的统一模型响应；流式期间通过 `on_text` 报告文本片段。
    流程：调用 Messages API → SDK 组装最终消息 → 转换内容和 usage。
    """

    client: Any
    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送 Anthropic 阻塞请求并归一化响应。"""
        response = self.client.messages.create(model=self.model, **kwargs)
        return _normalize_anthropic_response(response, self.model)

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """消费 Anthropic 文本流，并归一化 SDK 最终消息。"""
        with self.client.messages.stream(model=self.model, **kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    on_text(text)
            response = stream.get_final_message()
        return _normalize_anthropic_response(response, self.model)


def _normalize_anthropic_response(response: Any, fallback_model: str) -> ModelResponse:
    """把 Anthropic SDK 消息转成统一响应。"""
    usage = _get(response, "usage") or {}
    return ModelResponse(
        content=s05._to_jsonable(_get(response, "content") or []),
        model=str(_get(response, "model") or fallback_model),
        stop_reason=_optional_text(_get(response, "stop_reason")),
        usage=ModelUsage(
            input_tokens=int(_get(usage, "input_tokens") or 0),
            output_tokens=int(_get(usage, "output_tokens") or 0),
            cache_read_tokens=int(_get(usage, "cache_read_input_tokens") or 0),
            cache_write_tokens=int(_get(usage, "cache_creation_input_tokens") or 0),
        ),
    )


@dataclass(frozen=True)
class OpenAIChatProvider:
    """把 OpenAI-compatible Chat Completions 适配为统一 ModelProvider。

    作用：支持 OpenAI、DeepSeek 等兼容接口，同时保持 Agent 内部消息格式不变。
    输入：兼容客户端、模型名称，以及项目内部 system/messages/tools 请求参数。
    输出：统一 `ModelResponse`；OpenAI function call 会转换为内部 `tool_use`。
    流程：转换消息和工具 schema → 调用 Chat Completions → 组装文本与工具增量
    → 转回项目内部内容块和 usage。
    """

    client: Any
    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送 OpenAI-compatible 阻塞请求并归一化响应。"""
        request = _to_openai_request(kwargs)
        response = self.client.chat.completions.create(model=self.model, **request)
        return _normalize_openai_response(response, self.model)

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """消费 Chat Completions 增量并组装统一最终响应。"""
        request = _to_openai_request(kwargs)
        chunks = self.client.chat.completions.create(
            model=self.model,
            stream=True,
            stream_options={"include_usage": True},
            **request,
        )

        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        response_model = self.model
        stop_reason: str | None = None
        usage = ModelUsage()

        for chunk in chunks:
            response_model = str(_get(chunk, "model") or response_model)
            if chunk_usage := _get(chunk, "usage"):
                usage = _openai_usage(chunk_usage)

            choices = _get(chunk, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            stop_reason = _optional_text(_get(choice, "finish_reason")) or stop_reason
            delta = _get(choice, "delta") or {}

            if text := _get(delta, "content"):
                text = str(text)
                text_parts.append(text)
                on_text(text)

            # s10 新增：工具名称和 JSON 参数可能分散在多个流片段中，按 index 累加。
            for tool_delta in _get(delta, "tool_calls") or []:
                _append_openai_tool_delta(tool_calls, tool_delta)

        # s10 修复 B017：参考 Pi 默认结束标记检查，合法 JSON 不代表流已经完成。
        if stop_reason is None:
            raise ModelResponseError("OpenAI-compatible 流结束时缺少 finish_reason", usage)
        try:
            content = _openai_content_blocks("".join(text_parts), tool_calls.values())
        except (RuntimeError, TypeError, ValueError) as error:
            # s10 修复 B020：转换失败也保留流末尾已经返回的用量。
            raise ModelResponseError(str(error), usage) from error
        return ModelResponse(
            content=content,
            model=response_model,
            stop_reason=stop_reason,
            usage=usage,
        )


def _to_openai_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把项目内部请求参数转成 Chat Completions 参数。"""
    request = dict(kwargs)
    system = str(request.pop("system", ""))
    messages = request.pop("messages", [])
    tools = request.pop("tools", [])
    request["messages"] = _to_openai_messages(system, messages)
    # s10 新增：内部摘要不提供工具；此时省略字段，避免兼容服务拒绝空 tools 数组。
    if tools:
        request["tools"] = [_to_openai_tool(tool) for tool in tools]
    return request


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """把项目内部消息历史转换为 Chat Completions 消息。"""
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})

    for message in messages:
        role = str(message["role"])
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            converted.append(_to_openai_assistant_message(blocks))
            continue

        text_parts = [
            str(_get(block, "text"))
            for block in blocks
            if _get(block, "type") == "text" and _get(block, "text")
        ]
        if text_parts:
            converted.append({"role": role, "content": "\n".join(text_parts)})
        for block in blocks:
            if _get(block, "type") == "tool_result":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(_get(block, "tool_use_id") or ""),
                        "content": str(_get(block, "content") or ""),
                    }
                )
    return converted


def _to_openai_assistant_message(blocks: list[Any]) -> dict[str, Any]:
    """转换一条可能同时包含文本和工具调用的 assistant 消息。"""
    text = "\n".join(
        str(_get(block, "text"))
        for block in blocks
        if _get(block, "type") == "text" and _get(block, "text")
    )
    tool_calls = []
    for block in blocks:
        if _get(block, "type") != "tool_use":
            continue
        tool_calls.append(
            {
                "id": str(_get(block, "id") or ""),
                "type": "function",
                "function": {
                    "name": str(_get(block, "name") or ""),
                    "arguments": json.dumps(
                        _get(block, "input") or {},
                        ensure_ascii=False,
                    ),
                },
            }
        )

    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """把 Anthropic 风格工具 schema 转成 OpenAI function tool。"""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object"}),
        },
    }


def _normalize_openai_response(response: Any, fallback_model: str) -> ModelResponse:
    """把非流式 Chat Completions 响应转成统一响应。"""
    # s10 修复 B020：先取得用量，后续 choices 或工具参数转换失败时仍可报告。
    usage = _openai_usage(_get(response, "usage") or {})
    choices = _get(response, "choices") or []
    if not choices:
        raise ModelResponseError("OpenAI-compatible 模型没有返回 choices", usage)

    choice = choices[0]
    message = _get(choice, "message") or {}
    tool_calls: list[dict[str, str]] = []
    for index, tool_call in enumerate(_get(message, "tool_calls") or []):
        function = _get(tool_call, "function") or {}
        tool_calls.append(
            {
                "index": str(index),
                "id": str(_get(tool_call, "id") or ""),
                "name": str(_get(function, "name") or ""),
                "arguments": str(_get(function, "arguments") or "{}"),
            }
        )

    try:
        content = _openai_content_blocks(str(_get(message, "content") or ""), tool_calls)
    except (RuntimeError, TypeError, ValueError) as error:
        # s10 修复 B020：异常沿用 RuntimeError 契约，额外携带原响应的用量。
        raise ModelResponseError(str(error), usage) from error
    return ModelResponse(
        content=content,
        model=str(_get(response, "model") or fallback_model),
        stop_reason=_optional_text(_get(choice, "finish_reason")),
        usage=usage,
    )


def _append_openai_tool_delta(
    tool_calls: dict[int, dict[str, str]],
    tool_delta: Any,
) -> None:
    """把一个工具调用增量追加到对应 index 的缓冲区。"""
    index = int(_get(tool_delta, "index") or 0)
    current = tool_calls.setdefault(
        index,
        {"index": str(index), "id": "", "name": "", "arguments": ""},
    )
    if tool_id := _get(tool_delta, "id"):
        current["id"] = str(tool_id)
    function = _get(tool_delta, "function") or {}
    if name := _get(function, "name"):
        current["name"] += str(name)
    if arguments := _get(function, "arguments"):
        current["arguments"] += str(arguments)


def _openai_content_blocks(
    text: str,
    tool_calls: Any,
) -> list[dict[str, Any]]:
    """把 OpenAI 文本和 function calls 转成内部内容块。"""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for tool_call in tool_calls:
        arguments_text = str(tool_call.get("arguments") or "{}")
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"OpenAI-compatible 工具参数不是有效 JSON：{arguments_text}"
            ) from error
        content.append(
            {
                "type": "tool_use",
                "id": str(tool_call.get("id") or ""),
                "name": str(tool_call.get("name") or ""),
                "input": arguments,
            }
        )
    return content


def _openai_usage(usage: Any) -> ModelUsage:
    """把 Chat Completions usage 转成统一 Token 用量。"""
    prompt_details = _get(usage, "prompt_tokens_details") or {}
    cached_tokens = int(_get(prompt_details, "cached_tokens") or 0)
    prompt_tokens = int(_get(usage, "prompt_tokens") or 0)
    return ModelUsage(
        input_tokens=max(0, prompt_tokens - cached_tokens),
        output_tokens=int(_get(usage, "completion_tokens") or 0),
        cache_read_tokens=cached_tokens,
    )


def summary_request_options(provider: ModelProvider) -> dict[str, Any]:
    """为当前接口选择摘要请求选项，不修改正常回答配置。

    输入：已创建的 ModelProvider。
    输出：Anthropic 使用显式关闭思考；其他 Provider 返回空选项，不猜测其专有参数。
    流程：识别接口实现 → 选择摘要参数 → 由 main 传给 ContextSummarizer。
    边界：兼容服务可能不支持或忽略参数；不能仅凭模型名字判断接口能力。
    """
    if isinstance(provider, AnthropicProvider):
        return {"thinking": {"type": "disabled"}}
    return {}


def create_model_provider(
    *,
    config: ModelConfig | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> ModelProvider:
    """从显式配置创建模型 Provider，或适配原有环境调用方式。

    输入：ModelConfig；旧调用可提供 timeout_seconds/max_retries 并读取进程环境。
    输出：内置 Provider；构造客户端但不请求模型。
    流程：拒绝混用 → 兼容入口生成配置 → 显式传入已校验字段 → 返回适配器。
    边界：不读取 .env；高级认证与客户端配置仍可由调用方自行组装 Provider。
    """
    # s17 修改：保留原参数调用，但配置对象与旧参数不能各自提供一半配置。
    if config is not None:
        if not isinstance(config, ModelConfig):
            raise TypeError("config 必须是 ModelConfig")
        if timeout_seconds is not None or max_retries is not None:
            raise TypeError("config 不能与 timeout_seconds/max_retries 混用")
    else:
        values = dict(os.environ)
        if timeout_seconds is not None:
            values["MODEL_TIMEOUT_SECONDS"] = str(timeout_seconds)
        if max_retries is not None:
            values["MODEL_MAX_RETRIES"] = str(max_retries)
        config = model_config_from_env(values)

    # s17 修改：显式填入认证和默认地址，防止客户端再次用冲突环境覆盖这些配置字段。
    if config.provider == "anthropic":
        client = Anthropic(
            api_key=config.api_key or "",
            auth_token=config.auth_token or "",
            base_url=config.base_url or "https://api.anthropic.com",
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )
        return AnthropicProvider(client, config.model)

    # 来自 s10：保持；兼容接口仍延迟导入 OpenAI，不改变请求与响应归一化。
    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError(
            "使用 openai-compatible Provider 前请按 README 安装项目依赖。"
        ) from error
    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url or "https://api.openai.com/v1",
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )
    return OpenAIChatProvider(client, config.model)


def _get(value: Any, name: str) -> Any:
    """兼容读取对象、字典和测试替身中的字段。"""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text(value: Any) -> str | None:
    """把可选值规范为字符串或 None。"""
    return None if value is None else str(value)
