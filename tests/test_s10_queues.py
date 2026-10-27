"""s10：队列顺序、取消与末事件完成屏障，使用确定的异步门闩。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(params=["pi_code_agent", "tutorials.s10_queues"])
def api(request):
    return load_runtime(request.param)


@pytest.mark.parametrize("mode,counts", [("one-at-a-time", [1, 2]), ("all", [2])])
def test_initial_steering_modes_and_follow_up(api, mode, counts):
    async def scenario():
        model = ScriptedModel(*(api.AssistantMessage((), "stop") for _ in range(len(counts) + 1)))
        session = api.AgentSession(model=model, steering_mode=mode)
        session.steer("one")
        session.steer("two")
        session.follow_up("later")
        await session.prompt("start")
        for index, count in enumerate(counts):
            inputs = [m for m in model.calls[index]["messages"] if m.role == "user"]
            assert len(inputs) == count + 1
        assert model.calls[-1]["messages"][-1].content == "later"
        assert not session.busy

    asyncio.run(scenario())


def test_busy_delivery_waits_for_whole_tool_batch(api):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def first(args):
            entered.set()
            await release.wait()
            return api.ToolResult("first")

        second = AsyncMock(return_value=api.ToolResult("second"))
        model = ScriptedModel(
            api.AssistantMessage(
                (api.ToolCall("1", "first", {}), api.ToolCall("2", "second", {})), "tool_use"
            ),
            api.AssistantMessage((), "stop"),
            api.AssistantMessage((), "stop"),
        )
        session = api.AgentSession(
            model=model,
            tools=[api.Tool("first", "first", first), api.Tool("second", "second", second)],
        )
        task = asyncio.create_task(session.prompt("start"))
        await entered.wait()
        await session.send_message("hidden context", display=False, trigger_turn=False)
        await session.send_message("steer", trigger_turn=True)
        await session.send_message("follow", trigger_turn=True, deliver_as="follow_up")
        assert len(session.messages) == 2
        release.set()
        await task
        second.assert_awaited_once()
        assert [m.role for m in session.messages[:5]] == [
            "user",
            "assistant",
            "tool_result",
            "tool_result",
            "custom",
        ]
        assert model.calls[1]["messages"][-1].content == "steer"
        assert model.calls[2]["messages"][-1].content == "follow"

    asyncio.run(scenario())


def test_cancel_model_and_idle_waits_for_agent_end_observer(api):
    async def scenario():
        started, ending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Model:
            async def complete(self, **kwargs):
                started.set()
                await asyncio.Event().wait()

        session = api.AgentSession(model=Model())

        async def observer(event):
            if event["type"] == "agent_end":
                ending.set()
                await release.wait()

        session.subscribe(observer)
        run = asyncio.create_task(session.prompt("start"))
        await started.wait()
        waiter = asyncio.create_task(session.wait_for_idle())
        session.abort()
        await ending.wait()
        assert session.busy and not waiter.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        await waiter
        assert not session.busy and session.messages[-1].stop_reason == "aborted"
        session.abort()

    asyncio.run(scenario())


def test_cancel_tool_completes_pending_pairs_and_keeps_undelivered_queue(api):
    async def scenario():
        entered = asyncio.Event()

        async def tool(args):
            entered.set()
            await asyncio.Event().wait()

        model = ScriptedModel(
            api.AssistantMessage(
                (api.ToolCall("1", "run", {}), api.ToolCall("2", "run", {})), "tool_use"
            ),
            api.AssistantMessage((), "stop"),
        )
        session = api.AgentSession(model=model, tools=[api.Tool("run", "run", tool)])
        run = asyncio.create_task(session.prompt("run"))
        await entered.wait()
        session.steer("pending")
        session.abort()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert [m.tool_call_id for m in session.messages if m.role == "tool_result"] == ["1", "2"]
        assert all(m.is_error for m in session.messages if m.role == "tool_result")
        await session.continue_()
        assert model.calls[-1]["messages"][-1].content == "pending"

    asyncio.run(scenario())


def test_next_turn_is_not_delivered_until_next_prompt(api):
    async def scenario():
        model = ScriptedModel(api.AssistantMessage((), "stop"))
        session = api.AgentSession(model=model)
        await session.send_message("next", deliver_as="next_turn", trigger_turn=True)
        assert session.messages == [] and model.calls == []
        await session.prompt("go")
        assert [m.content for m in model.calls[0]["messages"]] == ["next", "go"]

    asyncio.run(scenario())
