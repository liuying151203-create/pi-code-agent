"""s05：真实协议转换和 SDK 累积器，使用离线流验证时序、最终回答与连接关闭。"""

import asyncio
from contextlib import aclosing
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from anthropic import NOT_GIVEN
from anthropic.lib.streaming import AsyncMessageStream
from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
)
from conftest import load_runtime


class RawStream:
    """模拟网络产生的原始块，保留真实 SDK 和 Provider 的解析与资源管理。"""

    def __init__(self, events, waiting=None):
        self.events = events
        self.waiting = waiting
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event
        if self.waiting is not None:
            self.waiting.set()
            await asyncio.Event().wait()

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def anthropic_events(*, tool=False, finish=True, reason=None):
    """创建真实 Anthropic 原始事件类型，交给 SDK 累积成最终消息。"""
    events = [
        RawMessageStartEvent(
            type="message_start",
            message={
                "id": "message",
                "type": "message",
                "role": "assistant",
                "model": "fake",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        )
    ]
    if tool:
        events += [
            RawContentBlockStartEvent(
                type="content_block_start",
                index=0,
                content_block={"type": "tool_use", "id": "call", "name": "add", "input": {}},
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '{"a":2,'},
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '"b":3}'},
            ),
        ]
    else:
        events += [
            RawContentBlockStartEvent(
                type="content_block_start", index=0, content_block={"type": "text", "text": ""}
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta", index=0, delta={"type": "text_delta", "text": "你"}
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta", index=0, delta={"type": "text_delta", "text": "好"}
            ),
        ]
    if finish:
        events += [
            RawContentBlockStopEvent(type="content_block_stop", index=0),
            RawMessageDeltaEvent(
                type="message_delta",
                delta={
                    "stop_reason": reason or ("tool_use" if tool else "end_turn"),
                    "stop_sequence": None,
                },
                usage={"output_tokens": 5},
            ),
            RawMessageStopEvent(type="message_stop"),
        ]
    return events


def chat(content=None, calls=None, reason=None, usage=None):
    return NS(
        choices=[
            NS(
                index=0,
                delta=NS(content=content, tool_calls=calls, refusal=None),
                finish_reason=reason,
            )
        ],
        usage=usage,
    )


def tool_piece(arguments, *, first=False, index=0):
    return NS(
        index=index,
        id="call" if first else None,
        type="function" if first else None,
        function=NS(name="add" if first else None, arguments=arguments),
    )


def chat_events(*, tool=False, finish=True, reason=None):
    events = (
        [chat(calls=[tool_piece('{"a":2,', first=True)]), chat(calls=[tool_piece('"b":3}')])]
        if tool
        else [chat("你"), chat("好")]
    )
    if finish:
        events += [
            chat(reason=reason or ("tool_calls" if tool else "stop")),
            NS(choices=[], usage=NS(prompt_tokens=11, completion_tokens=5)),
        ]
    return events


def model_for(api, kind, streams, **config):
    """仅替换网络，Anthropic 仍使用真实 SDK 的 AsyncMessageStream。"""
    if kind == "anthropic":
        constructor = Mock(
            side_effect=[AsyncMessageStream(stream, NOT_GIVEN) for stream in streams]
        )
        client = NS(messages=NS(stream=constructor), close=AsyncMock())
        model = api.AnthropicModel(api.ModelConfig(kind, "model", "fake", **config), client=client)
    else:
        constructor = AsyncMock(side_effect=streams)
        client = NS(chat=NS(completions=NS(create=constructor)), close=AsyncMock())
        model = api.OpenAIModel(api.ModelConfig(kind, "model", "fake", **config), client=client)
    return model, constructor, client


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s05_streaming",
        "tutorials.s06_sessions",
        "tutorials.s07_extensions",
    ]
)
def api(request):
    return load_runtime(request.param)


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_real_deltas_precede_message_end_and_one_final_history_message(api, kind):
    raw = RawStream(anthropic_events() if kind == "anthropic" else chat_events())
    model, request, client = model_for(api, kind, [raw])
    bus = api.EventBus()
    seen = []
    history = []

    def observe(event):
        seen.append(event)
        if event["type"] == "message_update":
            assert history[-1] == event["message"]
            assert history[-1].stop_reason == "pending"
            assert not raw.closed
        if event["type"] == "message_end":
            assert raw.closed

    bus.subscribe(observe)
    result = asyncio.run(
        api.agent_loop(history, model=model, stream=True, events=bus, max_tokens=123)
    )
    assert result is history
    assert len(history) == 1
    assert history[-1].content == (api.Text("你好"),)
    assert history[-1].usage.input_tokens == 11
    assert history[-1].usage.output_tokens == 5
    updates = [
        event["assistant_message_event"] for event in seen if event["type"] == "message_update"
    ]
    assert [event["type"] for event in updates] == [
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
    ]
    assert [event["delta"] for event in updates if event["type"] == "text_delta"] == ["你", "好"]
    assert sum(event["type"] == "message_start" for event in seen) == 1
    assert raw.closed
    client.close.assert_not_called()
    assert request.call_args.kwargs["max_tokens"] == 123
    if kind != "anthropic":
        assert request.call_args.kwargs["stream"] is True
        assert request.call_args.kwargs["stream_options"] == {"include_usage": True}
    assert "tools" not in request.call_args.kwargs


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_tool_json_is_complete_and_stream_closed_before_execution(api, kind):
    first = RawStream(
        anthropic_events(tool=True) if kind == "anthropic" else chat_events(tool=True)
    )
    second = RawStream(anthropic_events() if kind == "anthropic" else chat_events())
    model, _, _ = model_for(api, kind, [first, second])
    values = []

    async def execute(arguments):
        assert first.closed
        values.append(arguments)
        return api.ToolResult(str(arguments["a"] + arguments["b"]))

    tool = api.Tool("add", "sum", execute, api.AddArguments)
    history = asyncio.run(api.agent_loop([], model=model, tools=[tool], stream=True))
    assert values == [{"a": 2, "b": 3}]
    assert history[1] == api.ToolResultMessage("call", "add", "5")
    assert second.closed


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
@pytest.mark.parametrize("failure", ["network", "eof", "cancel", "observer"])
def test_interrupted_stream_preserves_actual_text_closes_and_never_executes(api, kind, failure):
    async def run():
        events = (
            anthropic_events(finish=False) if kind == "anthropic" else chat_events(finish=False)
        )
        if failure == "network":
            events += [OSError("wire broke")]
        waiting = asyncio.Event() if failure == "cancel" else None
        raw = RawStream(events, waiting=waiting)
        model, _, _ = model_for(api, kind, [raw])
        bus = api.EventBus()
        seen = []
        bus.subscribe(seen.append)
        execute = AsyncMock()

        def observer(event):
            if (
                failure == "observer"
                and event["type"] == "message_update"
                and event["assistant_message_event"]["type"] == "text_delta"
            ):
                raise RuntimeError("display broke")

        bus.subscribe(observer)
        history = []
        task = asyncio.create_task(
            api.agent_loop(
                history,
                model=model,
                tools=[api.Tool("probe", "probe", execute)],
                stream=True,
                events=bus,
            )
        )
        if waiting is not None:
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
        exception = (
            asyncio.CancelledError
            if failure == "cancel"
            else api.EventListenerError
            if failure == "observer"
            else api.ProviderError
        )
        with pytest.raises(exception):
            await asyncio.wait_for(task, 2)
        assert raw.closed
        assert len(history) == 1
        assert "".join(
            block.text for block in history[0].content if isinstance(block, api.Text)
        ).startswith("你")
        assert history[0].stop_reason == ("aborted" if failure == "cancel" else "error")
        execute.assert_not_called()
        if failure != "observer":
            assert seen[-1]["type"] == "agent_end"
            assert seen[-2]["type"] == "turn_end"
            assert sum(event["type"] == "message_start" for event in seen) == 1

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_direct_sdk_consumer_can_close_early(api, kind):
    raw = RawStream(anthropic_events() if kind == "anthropic" else chat_events())
    model, _, _ = model_for(api, kind, [raw])

    async def run():
        async with aclosing(model.stream(system="", messages=[], tools=[], max_tokens=9)) as stream:
            async for event in stream:
                if event["type"] == "text_delta":
                    break

    asyncio.run(run())
    assert raw.closed


def test_usage_option_can_be_disabled_and_token_parameter_is_real(api):
    raw = RawStream(chat_events())
    model, request, _ = model_for(
        api,
        "openai-compatible",
        [raw],
        stream_include_usage=False,
        token_parameter="max_completion_tokens",
    )
    asyncio.run(api.agent_loop([], model=model, stream=True, max_tokens=77))
    assert "stream_options" not in request.call_args.kwargs
    assert request.call_args.kwargs["max_completion_tokens"] == 77
    assert "max_tokens" not in request.call_args.kwargs


def test_complete_only_model_remains_supported_and_cannot_claim_streaming(api):
    model = NS(complete=AsyncMock(return_value=api.AssistantMessage((api.Text("whole"),))))
    assert asyncio.run(api.agent_loop([], model=model))[-1].content == (api.Text("whole"),)
    with pytest.raises(ValueError, match="stream"):
        asyncio.run(api.agent_loop([], model=model, stream=True))
    assert model.complete.await_count == 1


@pytest.mark.parametrize(
    "invalid", ["missing_done", "duplicate_start", "bad_start", "pending_final", "after_done"]
)
def test_stream_contract_failure_cannot_execute_tools(api, invalid):
    async def events(**kwargs):
        start = {"type": "start", "partial": api.AssistantMessage((), "pending")}
        if invalid == "bad_start":
            yield {"type": "start", "partial": None}
            return
        yield start
        if invalid == "duplicate_start":
            yield start
        elif invalid == "pending_final":
            yield {"type": "done", "message": api.AssistantMessage((), "pending")}
        elif invalid == "after_done":
            yield {
                "type": "done",
                "message": api.AssistantMessage((api.ToolCall("a", "probe", {}),), "tool_use"),
            }
            yield start

    execute = AsyncMock()
    history = []
    with pytest.raises(ValueError):
        asyncio.run(
            api.agent_loop(
                history,
                model=NS(stream=events),
                tools=[api.Tool("probe", "probe", execute)],
                stream=True,
            )
        )
    assert len(history) == 1
    assert history[-1].stop_reason == "error"
    execute.assert_not_called()


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
@pytest.mark.parametrize("bad", ["length", "invalid_json"])
def test_truncated_or_malformed_tool_calls_never_execute(api, kind, bad):
    if kind == "anthropic":
        events = anthropic_events(tool=True, reason="max_tokens" if bad == "length" else "tool_use")
        if bad == "invalid_json":
            events[3] = RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '"b":3'},
            )
    else:
        events = chat_events(tool=True, reason="length" if bad == "length" else "tool_calls")
        if bad == "invalid_json":
            events[1] = chat(calls=[tool_piece('"b":3')])
    first = RawStream(events)
    second = RawStream(anthropic_events() if kind == "anthropic" else chat_events())
    model, _, _ = model_for(api, kind, [first, second])
    execute = AsyncMock()
    history = asyncio.run(
        api.agent_loop(
            [], model=model, tools=[api.Tool("add", "sum", execute, api.AddArguments)], stream=True
        )
    )
    assert history[1].is_error
    execute.assert_not_called()
    assert first.closed and second.closed


def test_interleaved_chat_tool_indices_keep_source_order(api):
    events = [
        chat(
            calls=[
                NS(
                    index=1,
                    id="second",
                    type="function",
                    function=NS(name="add", arguments='{"a":4,'),
                )
            ]
        ),
        chat(
            calls=[
                NS(
                    index=0,
                    id="first",
                    type="function",
                    function=NS(name="add", arguments='{"a":2,'),
                )
            ]
        ),
        chat(
            calls=[
                NS(index=0, id=None, type=None, function=NS(name=None, arguments='"b":3}')),
                NS(index=1, id=None, type=None, function=NS(name=None, arguments='"b":5}')),
            ]
        ),
        chat(reason="tool_calls"),
    ]
    model, _, _ = model_for(api, "openai-compatible", [RawStream(events), RawStream(chat_events())])
    history = asyncio.run(api.agent_loop([], model=model, tools=[api.add_tool()], stream=True))
    results = [message for message in history if isinstance(message, api.ToolResultMessage)]
    assert [(result.tool_call_id, result.content) for result in results] == [
        ("first", "5"),
        ("second", "9"),
    ]


def test_anthropic_multiple_text_blocks_keep_content_indices(api):
    events = anthropic_events()
    # 第二个文本块插在消息结束前，用真实 SDK 保留两个块的边界。
    events[-2:-2] = [
        RawContentBlockStartEvent(
            type="content_block_start", index=1, content_block={"type": "text", "text": ""}
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=1, delta={"type": "text_delta", "text": "世界"}
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=1),
    ]
    model, _, _ = model_for(api, "anthropic", [RawStream(events)])
    bus = api.EventBus()
    updates = []
    bus.subscribe(
        lambda event: updates.append(event) if event["type"] == "message_update" else None
    )
    history = asyncio.run(api.agent_loop([], model=model, stream=True, events=bus))
    assert history[-1].content == (api.Text("你好"), api.Text("世界"))
    deltas = [
        event["assistant_message_event"]
        for event in updates
        if event["assistant_message_event"]["type"] == "text_delta"
    ]
    assert [event["content_index"] for event in deltas] == [0, 0, 1]


def test_console_displays_delta_before_final_without_duplicate_body(api, capsys):
    async def stream(**kwargs):
        yield {"type": "start", "partial": api.AssistantMessage((), "pending")}
        yield {
            "type": "text_delta",
            "content_index": 0,
            "delta": "你",
            "partial": api.AssistantMessage((api.Text("你"),), "pending"),
        }
        assert capsys.readouterr().out == "你"
        yield {
            "type": "text_delta",
            "content_index": 0,
            "delta": "好",
            "partial": api.AssistantMessage((api.Text("你好"),), "pending"),
        }
        yield {"type": "done", "message": api.AssistantMessage((api.Text("你好"),))}

    bus = api.EventBus()
    bus.subscribe(api.cli.ConsoleDisplay())
    history = asyncio.run(api.agent_loop([], model=NS(stream=stream), stream=True, events=bus))
    assert capsys.readouterr().out == "好\n"
    assert history[-1].content == (api.Text("你好"),)


def test_cli_can_choose_complete_mode_and_usage_config_is_explicit(
    api, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("whole mode", encoding="utf-8")
    model = NS(complete=AsyncMock(return_value=api.AssistantMessage((api.Text("whole answer"),))))
    monkeypatch.setattr(api.cli, "DemoModel", lambda *args, **kwargs: model)
    assert (
        api.cli.main(
            ["--demo", "--no-stream", "--workspace", str(tmp_path), "--prompt", "hello"],
            loop=api.agent_loop,
        )
        == 0
    )
    assert capsys.readouterr().out == "whole answer\n"
    model.complete.assert_awaited_once()
    config_file = tmp_path / ".env"
    config_file.write_text(
        "MODEL_PROVIDER=openai-compatible\nMODEL_ID=m\nMODEL_API_KEY=fake\nMODEL_STREAM_INCLUDE_USAGE=false\n",
        encoding="utf-8",
    )
    assert api.load_model_config(config_file, environ={}).stream_include_usage is False
    with pytest.raises(ValueError, match="stream_include_usage"):
        api.ModelConfig("openai-compatible", "m", "fake", stream_include_usage="false")


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_model_failure_state_keeps_text_and_cannot_execute_streamed_tool(api, kind):
    raw = RawStream(
        anthropic_events(tool=True, reason="refusal")
        if kind == "anthropic"
        else chat_events(tool=True, reason="content_filter")
    )
    model, _, _ = model_for(api, kind, [raw])
    execute = AsyncMock()
    history = asyncio.run(
        api.agent_loop(
            [], model=model, tools=[api.Tool("add", "sum", execute, api.AddArguments)], stream=True
        )
    )
    assert len(history) == 1
    assert history[-1].stop_reason == "error"
    execute.assert_not_called()


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_retry_keeps_failed_snapshot_locally_without_replaying_it_to_provider(api, kind):
    first = RawStream(
        (anthropic_events(finish=False) if kind == "anthropic" else chat_events(finish=False))
        + [OSError("interrupted")]
    )
    second = RawStream(anthropic_events() if kind == "anthropic" else chat_events())
    model, request, _ = model_for(api, kind, [first, second])
    history = [api.UserMessage("original")]
    with pytest.raises(api.ProviderError):
        asyncio.run(api.agent_loop(history, model=model, stream=True))
    failed = history[-1]
    assert failed.content == (api.Text("你好"),)
    asyncio.run(
        api.agent_loop(history, model=model, stream=True, prompts=[api.UserMessage("retry")])
    )
    assert history[1] is failed
    wire = request.call_args_list[-1].kwargs["messages"]
    # 默认系统提示只用于 Chat；重试的用户历史不包含失败回答。
    assert [message["role"] for message in wire if message["role"] != "system"] == ["user", "user"]


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_pending_history_is_rejected_before_opening_a_request(api, kind):
    model, request, _ = model_for(api, kind, [])

    async def run():
        async with aclosing(
            model.stream(
                system="", messages=[api.AssistantMessage((), "pending")], tools=[], max_tokens=10
            )
        ) as stream:
            async for _ in stream:
                pass

    with pytest.raises(api.ProviderError, match="pending"):
        asyncio.run(run())
    request.assert_not_called()


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_cancellation_before_first_text_keeps_received_usage(api, kind):
    async def run():
        waiting = asyncio.Event()
        data = (
            anthropic_events(finish=False)[:1]
            if kind == "anthropic"
            else [NS(choices=[], usage=NS(prompt_tokens=11, completion_tokens=0))]
        )
        raw = RawStream(data, waiting=waiting)
        model, _, _ = model_for(api, kind, [raw])
        history = []
        task = asyncio.create_task(api.agent_loop(history, model=model, stream=True))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert history[-1].stop_reason == "aborted"
        assert history[-1].usage.input_tokens == 11
        assert raw.closed

    asyncio.run(run())
