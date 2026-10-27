"""s04：按 Pi 事件顺序验证完整循环、等待语义和中断边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


def test_pi_sequence_and_per_turn_payloads(api):
    events = api.EventBus()
    seen = []
    events.subscribe(seen.append)
    tool = api.Tool("probe", "probe", AsyncMock(return_value=api.ToolResult("value")))
    first = api.AssistantMessage((api.ToolCall("one", "probe", {}),), "tool_use")
    last = api.AssistantMessage((api.Text("done"),))
    model = ScriptedModel(first, last)
    old = api.UserMessage("old history")
    prompt = api.UserMessage("new prompt")
    history = [old]
    result = asyncio.run(
        api.agent_loop(history, model=model, tools=[tool], prompts=[prompt], events=events)
    )
    assert result is history
    assert [event["type"] for event in seen] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert [event["message"] for event in seen if event["type"] == "message_end"] == history[1:]
    assert model.calls[0]["messages"] == (old, prompt)
    turns = [event for event in seen if event["type"] == "turn_end"]
    assert turns[0]["message"] == first
    assert turns[0]["tool_results"] == [history[3]]
    assert turns[1]["tool_results"] == []
    assert seen[-1]["messages"] == history[1:]
    assert seen[7]["result"] == api.ToolResult("value")


def test_continuation_and_reuse_do_not_replay_existing_history(api):
    events = api.EventBus()
    seen = []
    events.subscribe(seen.append)
    history = [api.UserMessage("already recorded")]
    asyncio.run(
        api.agent_loop(
            history, model=ScriptedModel(api.AssistantMessage((api.Text("first"),))), events=events
        )
    )
    assert not any(event.get("message") == history[0] for event in seen)
    first_end = seen[-1]
    seen.clear()
    asyncio.run(
        api.agent_loop(
            history,
            model=ScriptedModel(api.AssistantMessage((api.Text("second"),))),
            prompts=[api.UserMessage("next")],
            events=events,
        )
    )
    assert first_end["messages"] == [api.AssistantMessage((api.Text("first"),))]
    assert seen[-1]["messages"] == history[-2:]


def test_subscribers_are_awaited_in_order_before_tools_and_run_settlement(api):
    async def run():
        bus = api.EventBus()
        message_waiting = asyncio.Event()
        allow_tool = asyncio.Event()
        end_waiting = asyncio.Event()
        allow_end = asyncio.Event()
        order = []
        execute = AsyncMock(return_value=api.ToolResult("ok"))

        async def first(event):
            if (
                event["type"] == "message_end"
                and isinstance(event["message"], api.AssistantMessage)
                and event["message"].stop_reason == "tool_use"
            ):
                message_waiting.set()
                await allow_tool.wait()
            if event["type"] == "agent_end":
                end_waiting.set()
                await allow_end.wait()
                order.append("first ended")

        def second(event):
            if event["type"] == "agent_end":
                order.append("second ended")

        bus.subscribe(first)
        bus.subscribe(second)
        model = ScriptedModel(
            api.AssistantMessage((api.ToolCall("a", "probe", {}),), "tool_use"),
            api.AssistantMessage((api.Text("done"),)),
        )
        task = asyncio.create_task(
            api.agent_loop([], model=model, tools=[api.Tool("probe", "probe", execute)], events=bus)
        )
        await asyncio.wait_for(message_waiting.wait(), 2)
        execute.assert_not_called()
        allow_tool.set()
        await asyncio.wait_for(end_waiting.wait(), 2)
        assert not task.done()
        assert order == []
        allow_end.set()
        await asyncio.wait_for(task, 2)
        assert order == ["first ended", "second ended"]
        execute.assert_awaited_once()

    asyncio.run(run())


def test_subscription_deduplication_removal_and_next_event_changes(api):
    bus = api.EventBus()
    seen = []
    unsubscribe = bus.subscribe(seen.append)
    bus.subscribe(seen.append)
    assert seen == []
    asyncio.run(bus.emit({"type": "agent_start"}))
    assert len(seen) == 1
    unsubscribe()
    unsubscribe()
    asyncio.run(bus.emit({"type": "agent_start"}))
    assert len(seen) == 1

    def add_listener(event):
        bus.subscribe(seen.append)

    bus.subscribe(add_listener)
    asyncio.run(bus.emit({"type": "turn_start"}))
    assert len(seen) == 1
    asyncio.run(bus.emit({"type": "turn_start"}))
    assert len(seen) == 2


def test_observer_cannot_rewrite_arguments_or_other_observers(api):
    bus = api.EventBus()
    observed = []

    def mutate(event):
        if event["type"] == "message_end" and isinstance(event["message"], api.AssistantMessage):
            for block in event["message"].content:
                if isinstance(block, api.ToolCall):
                    block.arguments["a"] = 999
        if event["type"] == "tool_execution_start":
            event["args"]["a"] = 999
        if event["type"] == "agent_end":
            event["messages"].clear()

    bus.subscribe(mutate)
    bus.subscribe(observed.append)
    execute = AsyncMock(return_value=api.ToolResult("5"))
    model = ScriptedModel(
        api.AssistantMessage((api.ToolCall("a", "add", {"a": 2, "b": 3}),), "tool_use"),
        api.AssistantMessage((api.Text("done"),)),
    )
    history = asyncio.run(
        api.agent_loop(
            [], model=model, tools=[api.Tool("add", "sum", execute, api.AddArguments)], events=bus
        )
    )
    execute.assert_awaited_once_with({"a": 2, "b": 3})
    start = next(event for event in observed if event["type"] == "tool_execution_start")
    assert start["args"] == {"a": 2, "b": 3}
    assert len(observed[-1]["messages"]) == len(history)
    assert history[0].content[0].arguments["a"] == 2


@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_failed_model_reply_closes_lifecycle_without_tool_attempts(api, reason):
    bus = api.EventBus()
    seen = []
    bus.subscribe(seen.append)
    execute = AsyncMock()
    reply = api.AssistantMessage((api.ToolCall("a", "probe", {}),), reason)
    asyncio.run(
        api.agent_loop(
            [], model=ScriptedModel(reply), tools=[api.Tool("probe", "probe", execute)], events=bus
        )
    )
    assert [event["type"] for event in seen] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert seen[-2]["tool_results"] == []
    execute.assert_not_called()


@pytest.mark.parametrize("reason", ["tool_use", "length"])
def test_rejected_tool_attempts_have_end_and_message_events(api, reason):
    bus = api.EventBus()
    seen = []
    bus.subscribe(seen.append)
    execute = AsyncMock()
    calls = (api.ToolCall("a", "missing", {}), api.ToolCall("b", "add", {"a": "wrong", "b": 3}))
    model = ScriptedModel(
        api.AssistantMessage(calls, reason), api.AssistantMessage((api.Text("done"),))
    )
    history = asyncio.run(
        api.agent_loop(
            [], model=model, tools=[api.Tool("add", "sum", execute, api.AddArguments)], events=bus
        )
    )
    starts = [event for event in seen if event["type"] == "tool_execution_start"]
    ends = [event for event in seen if event["type"] == "tool_execution_end"]
    assert [event["tool_call_id"] for event in starts] == ["a", "b"]
    assert [event["tool_call_id"] for event in ends] == ["a", "b"]
    assert all(event["is_error"] for event in ends)
    assert [event["message"] for event in seen if event["type"] == "message_end"] == history
    execute.assert_not_called()


def test_provider_exception_closes_events_and_still_propagates(api):
    bus = api.EventBus()
    seen = []
    bus.subscribe(seen.append)
    model = ScriptedModel()
    error = api.ProviderError("offline failure")
    model.complete = AsyncMock(side_effect=error)
    history = []
    with pytest.raises(api.ProviderError) as raised:
        asyncio.run(api.agent_loop(history, model=model, events=bus))
    assert raised.value is error
    assert history[-1].stop_reason == "error"
    assert history[-1].error_message == "offline failure"
    assert seen[-2]["type"] == "turn_end"
    assert seen[-1] == {"type": "agent_end", "messages": history}


@pytest.mark.parametrize("kind", ["not_message", "duplicate_ids"])
def test_invalid_model_contract_closes_failure_events(api, kind):
    reply = (
        None
        if kind == "not_message"
        else api.AssistantMessage(
            (api.ToolCall("same", "probe", {}), api.ToolCall("same", "probe", {})), "tool_use"
        )
    )
    bus = api.EventBus()
    seen = []
    bus.subscribe(seen.append)
    execute = AsyncMock()
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            api.agent_loop(
                [],
                model=ScriptedModel(reply),
                tools=[api.Tool("probe", "probe", execute)],
                events=bus,
            )
        )
    assert seen[-1]["type"] == "agent_end"
    assert seen[-2]["message"].stop_reason == "error"
    execute.assert_not_called()


@pytest.mark.parametrize("during", ["model", "message", "tool"])
def test_cancellation_preserves_history_and_finishes_run_events(api, during):
    async def run():
        bus = api.EventBus()
        waiting = asyncio.Event()
        seen = []
        bus.subscribe(seen.append)
        executed = []

        async def pause(event):
            if during == "message" and event["type"] == "message_end":
                waiting.set()
                await asyncio.Event().wait()

        async def execute(arguments):
            executed.append(True)
            waiting.set()
            await asyncio.Event().wait()

        async def complete(**kwargs):
            waiting.set()
            await asyncio.Event().wait()

        if during == "message":
            unsubscribe = bus.subscribe(pause)
        model = ScriptedModel(
            api.AssistantMessage(
                (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "tool_use"
            )
        )
        if during == "model":
            model.complete = complete
        history = []
        task = asyncio.create_task(
            api.agent_loop(
                history, model=model, tools=[api.Tool("probe", "probe", execute)], events=bus
            )
        )
        await asyncio.wait_for(waiting.wait(), 2)
        if during == "message":
            unsubscribe()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert seen[-1]["type"] == "agent_end"
        assert seen[-2]["type"] == "turn_end"
        if during == "model":
            assert history[-1].stop_reason == "aborted"
        else:
            results = [message for message in history if isinstance(message, api.ToolResultMessage)]
            assert [result.tool_call_id for result in results] == ["a", "b"]
            assert all(result.is_error for result in results)
            assert len(executed) == (1 if during == "tool" else 0)
        assert seen[-1]["messages"] == history

    asyncio.run(run())


@pytest.mark.parametrize("at", ["message_end", "tool_execution_start", "tool_execution_end"])
def test_observer_failure_is_not_tool_failure_and_keeps_real_results(api, at):
    bus = api.EventBus()

    def fail(event):
        if event["type"] == at:
            raise RuntimeError("display failed")

    bus.subscribe(fail)
    execute = AsyncMock(return_value=api.ToolResult("actual output"))
    model = ScriptedModel(
        api.AssistantMessage(
            (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "tool_use"
        )
    )
    history = []
    with pytest.raises(api.EventListenerError, match="display failed") as raised:
        asyncio.run(
            api.agent_loop(
                history, model=model, tools=[api.Tool("probe", "probe", execute)], events=bus
            )
        )
    assert isinstance(raised.value.__cause__, RuntimeError)
    results = [message for message in history if isinstance(message, api.ToolResultMessage)]
    assert [result.tool_call_id for result in results] == ["a", "b"]
    assert execute.await_count == (1 if at == "tool_execution_end" else 0)
    if at == "tool_execution_end":
        assert results[0] == api.ToolResultMessage("a", "probe", "actual output")
    assert results[-1].is_error


def test_turn_limit_has_final_event_without_claiming_success(api):
    bus = api.EventBus()
    seen = []
    bus.subscribe(seen.append)
    model = ScriptedModel(api.AssistantMessage((api.ToolCall("a", "probe", {}),), "tool_use"))
    with pytest.raises(api.TurnLimitError):
        asyncio.run(
            api.agent_loop(
                [],
                model=model,
                tools=[api.Tool("probe", "probe", AsyncMock(return_value=api.ToolResult("ok")))],
                max_turns=1,
                events=bus,
            )
        )
    assert seen[-1]["type"] == "agent_end"
    assert seen[-2]["type"] == "turn_end"


@pytest.fixture(params=["pi_code_agent", "tutorials.s04_events", "tutorials.s05_streaming"])
def api(request):
    return load_runtime(request.param)
