"""s01：按 Pi 循环测试覆盖工具回传、错误结束和截断边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.mark.parametrize("registered", [True, False])
def test_first_demo_requests_tool_without_executing_and_uses_actual_result(tmp_path, registered):
    from dataclasses import replace

    from tutorials.s01_agent_loop.code import agent_loop
    from tutorials.s01_agent_loop.demo import DemoModel, project_name_tool
    from tutorials.s01_agent_loop.messages import ToolCall, UserMessage

    model = DemoModel()
    tool = project_name_tool(tmp_path)
    execute = AsyncMock(wraps=tool.execute)
    tool = replace(tool, execute=execute)

    async def run():
        history = [UserMessage("当前项目叫什么？")]
        request = await model.complete(
            system="", messages=tuple(history), tools=[tool.definition()], max_tokens=100
        )
        assert isinstance(request.content[0], ToolCall)
        assert request.content[0].name == tool.name
        assert request.content[0].arguments == {}
        execute.assert_not_awaited()

        await agent_loop(history, model=model, tools=[tool] if registered else [])
        assert [message.role for message in history] == [
            "user",
            "assistant",
            "tool_result",
            "assistant",
        ]
        result = history[2]
        assert result.tool_call_id == history[1].content[0].id
        if registered:
            execute.assert_awaited_once_with({})
            assert result.content == tmp_path.name
            assert history[-1].content[0].text == f"工具返回：{tmp_path.name}"
        else:
            execute.assert_not_awaited()
            assert result.is_error
            assert history[-1].content[0].text == f"工具失败：{result.content}"

    asyncio.run(run())


def test_tool_result_reaches_the_next_model_request(api, capsys):
    execute = AsyncMock(return_value=api.ToolResult("real result"))
    tool = api.Tool("probe", "test tool", execute)
    model = ScriptedModel(
        api.AssistantMessage((api.ToolCall("one", "probe", {}),), "tool_use"),
        api.AssistantMessage((api.Text("done"),)),
    )
    history = [api.UserMessage("test")]
    result = asyncio.run(api.agent_loop(history, model=model, tools=[tool]))
    assert result is history
    assert execute.await_count == 1
    assert isinstance(model.calls[1]["messages"][-1], api.ToolResultMessage)
    assert model.calls[1]["messages"][-1].content == "real result"
    assert model.calls[1]["messages"][-1].tool_call_id == "one"
    assert len(model.calls[0]["messages"]) == 1
    assert capsys.readouterr().out == ""


def test_explicit_empty_tools_do_not_get_replaced(api):
    model = ScriptedModel(api.AssistantMessage((api.Text("hello"),)))
    asyncio.run(api.agent_loop([api.UserMessage("hi")], model=model, tools=[]))
    assert model.calls[0]["tools"] == []


@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_failed_model_message_is_recorded_without_executing_tools(api, reason):
    execute = AsyncMock()
    response = api.AssistantMessage(
        (api.ToolCall("one", "probe", {}), api.ToolCall("one", "probe", {})), reason
    )
    history = asyncio.run(
        api.agent_loop(
            [], model=ScriptedModel(response), tools=[api.Tool("probe", "test", execute)]
        )
    )
    assert history == [response]
    execute.assert_not_called()


def test_all_calls_in_length_response_are_failed_without_execution(api):
    execute = AsyncMock()
    model = ScriptedModel(
        api.AssistantMessage(
            (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "length"
        ),
        api.AssistantMessage((api.Text("retry complete"),)),
    )
    result = asyncio.run(
        api.agent_loop([], model=model, tools=[api.Tool("probe", "test", execute)])
    )
    assert [
        message.tool_call_id for message in result if isinstance(message, api.ToolResultMessage)
    ] == ["a", "b"]
    assert all(message.is_error for message in result if isinstance(message, api.ToolResultMessage))
    execute.assert_not_called()


def test_same_batch_runs_in_source_order_and_preserves_error_status(api):
    order = []

    async def execute(arguments):
        order.append(len(order))
        if len(order) == 1:
            raise ValueError("first failed")
        return api.ToolResult("Error: this is ordinary successful text")

    model = ScriptedModel(
        api.AssistantMessage(
            (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "tool_use"
        ),
        api.AssistantMessage((api.Text("done"),)),
    )
    result = asyncio.run(
        api.agent_loop([], model=model, tools=[api.Tool("probe", "test", execute)])
    )
    results = [message for message in result if isinstance(message, api.ToolResultMessage)]
    assert order == [0, 1]
    assert [message.is_error for message in results] == [True, False]


def test_cancellation_propagates_and_finishes_unexecuted_tool_pairs(api):
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
            api.AssistantMessage(
                (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "tool_use"
            )
        )
        history = []
        task = asyncio.create_task(
            api.agent_loop(history, model=model, tools=[api.Tool("probe", "test", execute)])
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
        assert [
            message.tool_call_id
            for message in history
            if isinstance(message, api.ToolResultMessage)
        ] == ["a", "b"]

    asyncio.run(run())


def test_duplicate_tools_and_invalid_limits_fail_before_request(api):
    model = ScriptedModel()
    tool = api.Tool("same", "test", AsyncMock())
    for kwargs in ({"tools": [tool, tool]}, {"max_tokens": 0}, {"max_turns": True}):
        with pytest.raises(ValueError):
            asyncio.run(api.agent_loop([], model=model, **kwargs))
    assert model.calls == []


def test_explicit_turn_limit_keeps_completed_tool_results(api):
    model = ScriptedModel(api.AssistantMessage((api.ToolCall("one", "probe", {}),), "tool_use"))
    history = []
    with pytest.raises(api.TurnLimitError):
        asyncio.run(
            api.agent_loop(
                history,
                model=model,
                tools=[api.Tool("probe", "test", AsyncMock(return_value=api.ToolResult("ok")))],
                max_turns=1,
            )
        )
    assert isinstance(history[-1], api.ToolResultMessage)


def test_repeated_call_ids_are_rejected_before_tool_execution(api):
    execute = AsyncMock()
    model = ScriptedModel(
        api.AssistantMessage(
            (api.ToolCall("same", "probe", {}), api.ToolCall("same", "probe", {})), "tool_use"
        )
    )
    with pytest.raises(ValueError, match="ID"):
        asyncio.run(api.agent_loop([], model=model, tools=[api.Tool("probe", "test", execute)]))
    execute.assert_not_called()


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s01_agent_loop",
        "tutorials.s02_tools",
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
