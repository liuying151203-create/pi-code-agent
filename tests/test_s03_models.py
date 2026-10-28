"""s03：模拟真实 SDK 边界，验证两种协议对同一内部消息的转换。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import load_runtime


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


def test_anthropic_thinking_response_prints_only_final_text(api, tmp_path, monkeypatch, capsys):
    from anthropic.types import ThinkingBlock

    chapter = api
    config = chapter.ModelConfig("anthropic", "model", "fake")
    stub = client(
        anthropic(
            [
                ThinkingBlock(
                    type="thinking", thinking="internal reasoning", signature="opaque signature"
                ),
                NS(type="text", text="你好"),
            ]
        )
    )
    model = chapter.AnthropicModel(config, client=stub)
    monkeypatch.setattr(chapter.cli, "load_model_config", lambda path: config)
    monkeypatch.setattr(chapter.cli, "create_model", lambda value: model)
    arguments = ["--workspace", str(tmp_path), "--prompt", "你好"]
    if hasattr(chapter.AnthropicModel, "stream"):
        arguments.append("--no-stream")
    if hasattr(chapter, "AgentSession"):
        arguments.append("--no-session")
    if hasattr(chapter, "ExtensionRuntime"):
        arguments.append("--no-extensions")
    if hasattr(chapter, "ResourceLoader"):
        arguments.append("--no-resources")
    assert chapter.cli.main(arguments, loop=chapter.agent_loop) == 0
    output = capsys.readouterr().out
    assert (
        "你好" in output and "internal reasoning" not in output and "opaque signature" not in output
    )


def test_anthropic_thinking_and_redaction_survive_tool_round_trip(api):
    from anthropic.types import RedactedThinkingBlock, ThinkingBlock

    chapter = api
    blocks = [
        ThinkingBlock(type="thinking", thinking="  keep unchanged\n", signature="signed-data"),
        RedactedThinkingBlock(type="redacted_thinking", data="encrypted-data"),
        NS(type="tool_use", id="call", name="add", input={"a": "2", "b": "3"}),
    ]
    stub = client(
        anthropic(blocks, "tool_use"),
        anthropic(
            [
                ThinkingBlock(type="thinking", thinking="", signature="empty-text-signature"),
                NS(type="text", text="five"),
            ]
        ),
        anthropic([NS(type="text", text="done")]),
    )
    model = chapter.AnthropicModel(chapter.ModelConfig("anthropic", "model", "fake"), client=stub)

    async def run():
        history = [chapter.UserMessage("sum")]
        await chapter.agent_loop(history, model=model, tools=[chapter.add_tool()])
        assert history[-2].content == "5" and not history[-2].is_error
        replay = stub.messages.create.call_args.kwargs["messages"][1]["content"]
        assert replay == [
            {"type": "thinking", "thinking": "  keep unchanged\n", "signature": "signed-data"},
            {"type": "redacted_thinking", "data": "encrypted-data"},
            {"type": "tool_use", "id": "call", "name": "add", "input": {"a": "2", "b": "3"}},
        ]
        history.append(chapter.UserMessage("continue"))
        await chapter.agent_loop(history, model=model, tools=[chapter.add_tool()])
        replay = stub.messages.create.call_args.kwargs["messages"][-2]["content"]
        assert replay[0] == {
            "type": "thinking",
            "thinking": "",
            "signature": "empty-text-signature",
        }
        assert replay[1] == {"type": "text", "text": "five"}

    asyncio.run(run())


def test_unsigned_thinking_uses_pi_text_fallback_only_in_request(api):
    chapter = api
    stub = client(
        anthropic(
            [NS(type="thinking", thinking="unsigned reasoning"), NS(type="text", text="answer")]
        ),
        anthropic([NS(type="text", text="next answer")]),
    )
    model = chapter.AnthropicModel(chapter.ModelConfig("anthropic", "model", "fake"), client=stub)

    async def run():
        history = [chapter.UserMessage("first")]
        await chapter.agent_loop(history, model=model)
        history.append(chapter.UserMessage("next"))
        await chapter.agent_loop(history, model=model)
        blocks = stub.messages.create.call_args.kwargs["messages"][1]["content"]
        assert blocks == [
            {"type": "text", "text": "unsigned reasoning"},
            {"type": "text", "text": "answer"},
        ]
        assert type(history[1].content[0]).__name__ == "Thinking"

    asyncio.run(run())


@pytest.mark.parametrize("with_text", [False, True])
def test_chat_projection_does_not_send_anthropic_thinking(api, with_text):
    chapter = api
    content = [chapter.messages.Thinking("internal", "anthropic-signature")]
    if with_text:
        content.append(chapter.Text("visible answer"))
    history = [
        chapter.UserMessage("first"),
        chapter.AssistantMessage(tuple(content)),
        chapter.UserMessage("next"),
    ]
    stub = client(openai())
    model = chapter.OpenAIModel(
        chapter.ModelConfig("openai-compatible", "model", "fake"), client=stub
    )
    asyncio.run(model.complete(system="", messages=history, tools=[], max_tokens=100))
    sent = stub.messages.create.call_args.kwargs["messages"]
    assert len(sent) == (3 if with_text else 2)
    if with_text:
        assert sent[1] == {"role": "assistant", "content": "visible answer"}


def test_s03_demo_requests_read_and_uses_current_workspace_file(tmp_path):
    from tutorials.s03_models.demo import DemoModel

    chapter = load_runtime("tutorials.s03_models")
    path = tmp_path / "README.md"
    path.write_text("first content", encoding="utf-8")
    model = DemoModel()
    tool = chapter.read_tool(tmp_path)

    async def run():
        history = [chapter.UserMessage("读取 README.md")]
        await chapter.agent_loop(history, model=model, tools=[tool])
        assert history[1].content[0].name == tool.name
        assert history[1].content[0].arguments == {"path": "README.md"}
        assert history[-2].content == "first content"
        path.write_text("updated content", encoding="utf-8")
        history.append(chapter.UserMessage("再读一次"))
        await chapter.agent_loop(history, model=model, tools=[tool])
        assert history[-2].content == "updated content"
        assert history[-1].content[0].text == "工具返回：updated content"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_failed_answers_stay_local_without_replaying_tool_calls(api, kind, reason):
    chapter = api
    failed = chapter.AssistantMessage(
        (chapter.Text("failed content"), chapter.ToolCall("unfinished", "read", {})), reason
    )
    history = [chapter.UserMessage("first"), failed, chapter.UserMessage("retry")]
    stub = client(anthropic([NS(type="text", text="done")]) if kind == "anthropic" else openai())
    factory = chapter.AnthropicModel if kind == "anthropic" else chapter.OpenAIModel
    model = factory(chapter.ModelConfig(kind, "model", "fake"), client=stub)
    asyncio.run(model.complete(system="", messages=history, tools=[], max_tokens=20))
    sent = stub.messages.create.call_args.kwargs["messages"]
    assert sent == [{"role": "user", "content": "first"}, {"role": "user", "content": "retry"}]
    assert len(history) == 3 and history[1] is failed
    assert history[1].content[0].text == "failed content"


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_same_loop_executes_tools_with_either_provider(api, kind):
    if kind == "anthropic":
        stub = client(
            anthropic(
                [NS(type="tool_use", id="call", name="add", input={"a": "2", "b": "3"})], "tool_use"
            ),
            anthropic([NS(type="text", text="five")]),
        )
        model = api.AnthropicModel(api.ModelConfig(kind, "model", "fake"), client=stub)
    else:
        call = NS(
            id="call", type="function", function=NS(name="add", arguments='{"a":"2","b":"3"}')
        )
        stub = client(openai(None, [call], "tool_calls"), openai("five"))
        model = api.OpenAIModel(api.ModelConfig(kind, "model", "fake"), client=stub)
    history = asyncio.run(
        api.agent_loop(
            [api.UserMessage("sum")], model=model, tools=[api.add_tool()], max_tokens=123
        )
    )
    assert history[-2] == api.ToolResultMessage("call", "add", "5")
    assert history[-1].content == (api.Text("five"),)
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
def test_batch_results_keep_ids_and_only_protocol_adapter_wraps_errors(api, kind):
    history = [
        api.UserMessage("test"),
        api.AssistantMessage(
            (api.ToolCall("a", "add", {}), api.ToolCall("b", "add", {})), "tool_use"
        ),
        api.ToolResultMessage("a", "add", "Error: successful text", False),
        api.ToolResultMessage("b", "add", "bad params", True),
    ]
    if kind == "anthropic":
        stub = client(anthropic([NS(type="text", text="done")]))
        model = api.AnthropicModel(api.ModelConfig(kind, "m", "fake"), client=stub)
    else:
        stub = client(openai())
        model = api.OpenAIModel(api.ModelConfig(kind, "m", "fake"), client=stub)
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


def test_malformed_openai_json_returns_tool_error_without_execution(api):
    call = NS(id="bad", type="function", function=NS(name="add", arguments='{"a":'))
    stub = client(openai(None, [call], "tool_calls"), openai())
    model = api.OpenAIModel(api.ModelConfig("openai-compatible", "m", "fake"), client=stub)
    history = asyncio.run(api.agent_loop([], model=model, tools=[api.add_tool()]))
    assert history[-2].is_error
    assert isinstance(history[0].content[0].arguments, str)


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_request_failure_and_cancellation_remain_model_failures(api, kind):
    factory = api.AnthropicModel if kind == "anthropic" else api.OpenAIModel
    config = api.ModelConfig(kind, "m", "fake")
    stub = client()
    stub.messages.create.side_effect = OSError("offline")
    model = factory(config, client=stub)
    with pytest.raises(api.ProviderError, match="offline"):
        asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))
    stub.messages.create.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))


def test_client_options_budget_parameter_and_ownership(api, monkeypatch):
    stub = client(openai())
    constructor = Mock(return_value=stub)
    monkeypatch.setattr("openai.AsyncOpenAI", constructor)
    config = api.ModelConfig(
        "openai-compatible",
        "explicit-model",
        "fake",
        base_url="https://example.invalid/v1",
        timeout_seconds=12,
        max_retries=3,
        token_parameter="max_completion_tokens",
    )

    async def run():
        async with api.create_model(config) as model:
            await model.complete(system="", messages=[], tools=[], max_tokens=77)
        await model.aclose()

    asyncio.run(run())
    assert constructor.call_args.kwargs["timeout"] == 12
    assert constructor.call_args.kwargs["max_retries"] == 3
    assert constructor.call_args.kwargs["base_url"] == "https://example.invalid/v1"
    assert stub.messages.create.call_args.kwargs["max_completion_tokens"] == 77
    assert "max_tokens" not in stub.messages.create.call_args.kwargs
    stub.close.assert_awaited_once()


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_injected_client_remains_owned_by_caller(api, kind):
    factory = api.AnthropicModel if kind == "anthropic" else api.OpenAIModel
    config = api.ModelConfig(kind, "m", "fake")
    stub = client()
    asyncio.run(factory(config, client=stub).aclose())
    stub.close.assert_not_called()


def test_anthropic_call_ids_are_normalized_on_both_sides(api):
    stub = client(anthropic([NS(type="text", text="done")]))
    model = api.AnthropicModel(api.ModelConfig("anthropic", "m", "fake"), client=stub)
    history = [
        api.AssistantMessage((api.ToolCall("id|with:spaces", "probe", {}),), "tool_use"),
        api.ToolResultMessage("id|with:spaces", "probe", "done"),
    ]
    asyncio.run(model.complete(system="", messages=history, tools=[], max_tokens=1))
    converted = stub.messages.create.call_args.kwargs["messages"]
    assert converted[0]["content"][0]["id"] == "id_with_spaces"
    assert converted[1]["content"][0]["tool_use_id"] == "id_with_spaces"
    assert history[0].content[0].id == "id|with:spaces"


@pytest.mark.parametrize("reason", ["refusal", "sensitive"])
def test_anthropic_refusal_is_a_model_failure_message(api, reason):
    stub = client(anthropic([], reason))
    model = api.AnthropicModel(api.ModelConfig("anthropic", "m", "fake"), client=stub)
    result = asyncio.run(model.complete(system="", messages=[], tools=[], max_tokens=1))
    assert result.stop_reason == "error"
    assert reason in result.error_message


def test_unknown_openai_finish_reason_cannot_execute_calls(api):
    call = NS(id="x", type="function", function=NS(name="add", arguments='{"a":2,"b":3}'))
    stub = client(openai(None, [call], "new_failure_reason"))
    model = api.OpenAIModel(api.ModelConfig("openai-compatible", "m", "fake"), client=stub)
    history = asyncio.run(api.agent_loop([], model=model, tools=[api.add_tool()]))
    assert len(history) == 1
    assert history[0].stop_reason == "error"


def test_owned_client_closes_when_request_is_cancelled(api, monkeypatch):
    stub = client()
    stub.messages.create.side_effect = asyncio.CancelledError()
    constructor = Mock(return_value=stub)
    monkeypatch.setattr("anthropic.AsyncAnthropic", constructor)

    async def run():
        async with api.create_model(api.ModelConfig("anthropic", "m", "fake")) as model:
            await model.complete(system="", messages=[], tools=[], max_tokens=1)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    stub.close.assert_awaited_once()


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s03_models",
        "tutorials.s04_events",
        "tutorials.s05_streaming",
        "tutorials.s06_sessions",
        "tutorials.s07_extensions",
        "tutorials.s08_hooks",
        "tutorials.s09_commands",
        "tutorials.s10_queues",
        "tutorials.s11_resources",
        "tutorials.s12_reload",
    ]
)
def api(request):
    return load_runtime(request.param)
