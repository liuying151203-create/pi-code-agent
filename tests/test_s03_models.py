"""s03：模拟真实 SDK 边界，验证两种协议对同一内部消息的转换。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from pi_code_agent import (
    AssistantMessage,
    ModelConfig,
    Text,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    agent_loop,
    providers,
)
from pi_code_agent.demo import add_tool
from pi_code_agent.providers import AnthropicModel, OpenAIModel, ProviderError, create_model


def client(*responses):
    """只替代网络请求，保留真实 Provider、转换和循环。"""
    create = AsyncMock(side_effect=list(responses))
    return NS(messages=NS(create=create), chat=NS(completions=NS(create=create)), close=AsyncMock())


def anthropic(content, reason="end_turn"):
    return NS(content=content, stop_reason=reason, usage=NS(input_tokens=11, output_tokens=5))


def openai(content="done", calls=None, reason="stop"):
    return NS(
        choices=[
            NS(message=NS(content=content, tool_calls=calls, refusal=None), finish_reason=reason)
        ],
        usage=NS(prompt_tokens=11, completion_tokens=5),
    )


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_same_loop_executes_tools_with_either_provider(kind):
    if kind == "anthropic":
        stub = client(
            anthropic(
                [NS(type="tool_use", id="call", name="add", input={"a": "2", "b": "3"})], "tool_use"
            ),
            anthropic([NS(type="text", text="five")]),
        )
        model = AnthropicModel(ModelConfig(kind, "model", "fake"), client=stub)
    else:
        call = NS(
            id="call", type="function", function=NS(name="add", arguments='{"a":"2","b":"3"}')
        )
        stub = client(openai(None, [call], "tool_calls"), openai("five"))
        model = OpenAIModel(ModelConfig(kind, "model", "fake"), client=stub)
    history = asyncio.run(
        agent_loop([UserMessage("sum")], model=model, tools=[add_tool()], max_tokens=123)
    )
    assert history[-2] == ToolResultMessage("call", "add", "5")
    assert history[-1].content == (Text("five"),)
    assert history[-1].usage.input_tokens == 11
    requests = stub.messages.create.call_args_list
    assert len(requests) == 2
    assert requests[0].kwargs["max_tokens"] == 123
    messages = requests[1].kwargs["messages"]
    if kind == "anthropic":
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][0]["tool_use_id"] == "call"
        assert messages[-1]["content"][0]["content"] == "5"
    else:
        assert messages[-1] == {"role": "tool", "tool_call_id": "call", "content": "5"}


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_batch_results_keep_ids_and_only_protocol_adapter_wraps_errors(kind):
    history = [
        UserMessage("test"),
        AssistantMessage((ToolCall("a", "add", {}), ToolCall("b", "add", {})), "tool_use"),
        ToolResultMessage("a", "add", "Error: successful text", False),
        ToolResultMessage("b", "add", "bad params", True),
    ]
    if kind == "anthropic":
        stub = client(anthropic([NS(type="text", text="done")]))
        model = AnthropicModel(ModelConfig(kind, "m", "fake"), client=stub)
    else:
        stub = client(openai())
        model = OpenAIModel(ModelConfig(kind, "m", "fake"), client=stub)
    asyncio.run(model.complete(system="s", messages=history, tools=[], max_tokens=1))
    request = stub.messages.create.call_args.kwargs
    assert "tools" not in request
    if kind == "anthropic":
        results = request["messages"][-1]["content"]
        assert [item["tool_use_id"] for item in results] == ["a", "b"]
        assert [item["is_error"] for item in results] == [False, True]
    else:
        assert request["messages"][-2]["content"] == "Error: successful text"
        assert request["messages"][-1]["content"] == "bad params"
    assert history[-1].content == "bad params"


def test_malformed_openai_json_returns_tool_error_without_execution():
    call = NS(id="bad", type="function", function=NS(name="add", arguments='{"a":'))
    stub = client(openai(None, [call], "tool_calls"), openai())
    model = OpenAIModel(ModelConfig("openai-compatible", "m", "fake"), client=stub)
    history = asyncio.run(agent_loop([], model=model, tools=[add_tool()]))
    assert history[-2].is_error
    assert isinstance(history[0].content[0].arguments, str)


@pytest.mark.parametrize(
    "factory,config",
    [
        (AnthropicModel, ModelConfig("anthropic", "m", "fake")),
        (OpenAIModel, ModelConfig("openai-compatible", "m", "fake")),
    ],
)
def test_request_failure_and_cancellation_remain_model_failures(factory, config):
    stub = client()
    stub.messages.create.side_effect = OSError("offline")
    model = factory(config, client=stub)
    with pytest.raises(ProviderError, match="offline"):
        asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))
    stub.messages.create.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))


def test_client_options_budget_parameter_and_ownership(monkeypatch):
    stub = client(openai())
    constructor = Mock(return_value=stub)
    monkeypatch.setattr(providers, "AsyncOpenAI", constructor)
    config = ModelConfig(
        "openai-compatible",
        "explicit-model",
        "fake",
        base_url="https://example.invalid/v1",
        timeout_seconds=12,
        max_retries=3,
        token_parameter="max_completion_tokens",
    )

    async def run():
        async with create_model(config) as model:
            await model.complete(system="", messages=[], tools=[], max_tokens=77)
        await model.aclose()

    asyncio.run(run())
    assert constructor.call_args.kwargs["timeout"] == 12
    assert constructor.call_args.kwargs["max_retries"] == 3
    assert constructor.call_args.kwargs["base_url"] == "https://example.invalid/v1"
    assert stub.messages.create.call_args.kwargs["max_completion_tokens"] == 77
    assert "max_tokens" not in stub.messages.create.call_args.kwargs
    stub.close.assert_awaited_once()


@pytest.mark.parametrize(
    "factory,config",
    [
        (AnthropicModel, ModelConfig("anthropic", "m", "fake")),
        (OpenAIModel, ModelConfig("openai-compatible", "m", "fake")),
    ],
)
def test_injected_client_remains_owned_by_caller(factory, config):
    stub = client()
    asyncio.run(factory(config, client=stub).aclose())
    stub.close.assert_not_called()


def test_anthropic_call_ids_are_normalized_on_both_sides():
    stub = client(anthropic([NS(type="text", text="done")]))
    model = AnthropicModel(ModelConfig("anthropic", "m", "fake"), client=stub)
    history = [
        AssistantMessage((ToolCall("id|with:spaces", "probe", {}),), "tool_use"),
        ToolResultMessage("id|with:spaces", "probe", "done"),
    ]
    asyncio.run(model.complete(system="", messages=history, tools=[], max_tokens=1))
    converted = stub.messages.create.call_args.kwargs["messages"]
    assert converted[0]["content"][0]["id"] == "id_with_spaces"
    assert converted[1]["content"][0]["tool_use_id"] == "id_with_spaces"
    assert history[0].content[0].id == "id|with:spaces"


@pytest.mark.parametrize("reason", ["refusal", "sensitive"])
def test_anthropic_refusal_is_a_model_failure_message(reason):
    stub = client(anthropic([], reason))
    model = AnthropicModel(ModelConfig("anthropic", "m", "fake"), client=stub)
    result = asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))
    assert result.stop_reason == "error"
    assert reason in result.error_message


def test_unknown_openai_finish_reason_cannot_execute_calls():
    call = NS(id="x", type="function", function=NS(name="add", arguments='{"a":2,"b":3}'))
    stub = client(openai(None, [call], "new_failure_reason"))
    model = OpenAIModel(ModelConfig("openai-compatible", "m", "fake"), client=stub)
    history = asyncio.run(agent_loop([], model=model, tools=[add_tool()]))
    assert len(history) == 1
    assert history[0].stop_reason == "error"


def test_owned_client_closes_when_request_is_cancelled(monkeypatch):
    stub = client()
    stub.messages.create.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(providers, "AsyncAnthropic", Mock(return_value=stub))

    async def run():
        async with create_model(ModelConfig("anthropic", "m", "fake")) as model:
            await model.complete(system="", messages=[], tools=[], max_tokens=1)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    stub.close.assert_awaited_once()
