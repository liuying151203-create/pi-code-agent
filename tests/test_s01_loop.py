"""s01：按 Pi 循环测试覆盖工具回传、错误结束和截断边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel

from pi_code_agent import (
    AssistantMessage,
    Text,
    Tool,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    TurnLimitError,
    UserMessage,
)
from pi_code_agent import agent_loop as sdk_loop
from tutorials.s01_agent_loop.code import agent_loop as chapter_loop


@pytest.fixture(params=[sdk_loop, chapter_loop], ids=["sdk", "chapter"])
def loop(request):
    return request.param


def test_tool_result_reaches_the_next_model_request(loop, capsys):
    execute = AsyncMock(return_value=ToolResult("real result"))
    tool = Tool("probe", "test tool", execute)
    model = ScriptedModel(
        AssistantMessage((ToolCall("one", "probe", {}),), "tool_use"),
        AssistantMessage((Text("done"),)),
    )
    history = [UserMessage("test")]
    result = asyncio.run(loop(history, model=model, tools=[tool]))
    assert result is history
    assert execute.await_count == 1
    assert isinstance(model.calls[1]["messages"][-1], ToolResultMessage)
    assert model.calls[1]["messages"][-1].content == "real result"
    assert model.calls[1]["messages"][-1].tool_call_id == "one"
    assert len(model.calls[0]["messages"]) == 1
    assert capsys.readouterr().out == ""


def test_explicit_empty_tools_do_not_get_replaced(loop):
    model = ScriptedModel(AssistantMessage((Text("hello"),)))
    asyncio.run(loop([UserMessage("hi")], model=model, tools=[]))
    assert model.calls[0]["tools"] == []


@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_failed_model_message_is_recorded_without_executing_tools(loop, reason):
    execute = AsyncMock()
    response = AssistantMessage(
        (ToolCall("one", "probe", {}), ToolCall("one", "probe", {})), reason
    )
    history = asyncio.run(
        loop([], model=ScriptedModel(response), tools=[Tool("probe", "test", execute)])
    )
    assert history == [response]
    execute.assert_not_called()


def test_all_calls_in_length_response_are_failed_without_execution(loop):
    execute = AsyncMock()
    model = ScriptedModel(
        AssistantMessage((ToolCall("a", "probe", {}), ToolCall("b", "probe", {})), "length"),
        AssistantMessage((Text("retry complete"),)),
    )
    result = asyncio.run(loop([], model=model, tools=[Tool("probe", "test", execute)]))
    assert [
        message.tool_call_id for message in result if isinstance(message, ToolResultMessage)
    ] == ["a", "b"]
    assert all(message.is_error for message in result if isinstance(message, ToolResultMessage))
    execute.assert_not_called()


def test_same_batch_runs_in_source_order_and_preserves_error_status(loop):
    order = []

    async def execute(arguments):
        order.append(len(order))
        if len(order) == 1:
            raise ValueError("first failed")
        return ToolResult("Error: this is ordinary successful text")

    model = ScriptedModel(
        AssistantMessage((ToolCall("a", "probe", {}), ToolCall("b", "probe", {})), "tool_use"),
        AssistantMessage((Text("done"),)),
    )
    result = asyncio.run(loop([], model=model, tools=[Tool("probe", "test", execute)]))
    results = [message for message in result if isinstance(message, ToolResultMessage)]
    assert order == [0, 1]
    assert [message.is_error for message in results] == [True, False]


def test_cancellation_propagates_and_finishes_unexecuted_tool_pairs(loop):
    async def run():
        started = asyncio.Event()
        cleaned = []

        async def execute(arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        model = ScriptedModel(
            AssistantMessage((ToolCall("a", "probe", {}), ToolCall("b", "probe", {})), "tool_use")
        )
        history = []
        task = asyncio.create_task(
            loop(history, model=model, tools=[Tool("probe", "test", execute)])
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
        assert [
            message.tool_call_id for message in history if isinstance(message, ToolResultMessage)
        ] == ["a", "b"]

    asyncio.run(run())


def test_duplicate_tools_and_invalid_limits_fail_before_request(loop):
    model = ScriptedModel()
    tool = Tool("same", "test", AsyncMock())
    for kwargs in ({"tools": [tool, tool]}, {"max_tokens": 0}, {"max_turns": True}):
        with pytest.raises(ValueError):
            asyncio.run(loop([], model=model, **kwargs))
    assert model.calls == []


def test_explicit_turn_limit_keeps_completed_tool_results(loop):
    model = ScriptedModel(AssistantMessage((ToolCall("one", "probe", {}),), "tool_use"))
    history = []
    with pytest.raises(TurnLimitError):
        asyncio.run(
            loop(
                history,
                model=model,
                tools=[Tool("probe", "test", AsyncMock(return_value=ToolResult("ok")))],
                max_turns=1,
            )
        )
    assert isinstance(history[-1], ToolResultMessage)


def test_repeated_call_ids_are_rejected_before_tool_execution(loop):
    execute = AsyncMock()
    model = ScriptedModel(
        AssistantMessage((ToolCall("same", "probe", {}), ToolCall("same", "probe", {})), "tool_use")
    )
    with pytest.raises(ValueError, match="ID"):
        asyncio.run(loop([], model=model, tools=[Tool("probe", "test", execute)]))
    execute.assert_not_called()
