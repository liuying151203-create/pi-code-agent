"""s08：真实会话中验证 Hook 顺序、失败和持久化边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(params=["pi_code_agent", "tutorials.s08_hooks"])
def api(request):
    return load_runtime(request.param)


def test_input_context_are_chained_and_context_is_not_saved(api):
    async def scenario():
        runtime = api.ExtensionRuntime()

        def setup(ext):
            ext.on("input", lambda event, ctx: {"action": "transform", "text": event["text"] + "!"})
            ext.on("input", lambda event, ctx: {"action": "transform", "text": event["text"] + "?"})
            ext.on(
                "context",
                lambda event, ctx: {"messages": [*event["messages"], api.UserMessage("临时提示")]},
            )

        await runtime.register(setup)
        model = ScriptedModel(api.AssistantMessage((api.Text("ok"),), "stop"))
        session = api.AgentSession(model=model, extensions=runtime)
        await session.prompt("hello")
        assert session.messages[0].content == "hello!?"
        assert model.calls[0]["messages"][-1].content == "临时提示"
        assert len(session.messages) == 2
        runtime.close()

    asyncio.run(scenario())


def test_handled_does_not_request_model_or_append_user(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        await runtime.register(lambda ext: ext.on("input", lambda e, c: {"action": "handled"}))
        model = ScriptedModel()
        session = api.AgentSession(model=model, extensions=runtime)
        assert await session.prompt("handled") == []
        assert model.calls == [] and not session.busy
        runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("block,fail", [(True, False), (False, True), (False, False)])
def test_tool_policy_failure_blocks_and_result_can_override(api, block, fail):
    async def scenario():
        runtime = api.ExtensionRuntime()
        execute = AsyncMock(return_value=api.ToolResult("raw"))
        seen = []

        def before(e, c):
            seen.append(e["input"])
            if fail:
                raise RuntimeError("policy broken")
            return {"block": block, "reason": "denied"}

        def setup(ext):
            ext.on("tool_call", before)
            ext.on("tool_result", lambda e, c: {"content": e["content"] + "!", "terminate": True})

        await runtime.register(setup)
        model = ScriptedModel(api.AssistantMessage((api.ToolCall("a", "run", {}),), "tool_use"))
        session = api.AgentSession(
            model=model, tools=[api.Tool("run", "run", execute)], extensions=runtime
        )
        await session.prompt("run")
        assert seen == [{}]
        assert execute.await_count == (0 if block or fail else 1)
        assert session.messages[-1].content.endswith("!")
        assert session.messages[-1].is_error == (block or fail)
        assert len(model.calls) == 1
        runtime.close()

    asyncio.run(scenario())


def test_bad_arguments_skip_call_hook_but_result_hook_sees_error(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        call = AsyncMock()
        results = []

        def setup(ext):
            ext.on("tool_call", call)
            ext.on("tool_result", lambda e, c: results.append(e))

        await runtime.register(setup)
        model = ScriptedModel(
            api.AssistantMessage((api.ToolCall("a", "run", {"x": 1}),), "tool_use"),
            api.AssistantMessage((api.Text("done"),), "stop"),
        )
        await api.AgentSession(
            model=model, tools=[api.Tool("run", "run", AsyncMock())], extensions=runtime
        ).prompt("run")
        assert call.await_count == 0 and results[0]["is_error"]
        runtime.close()

    asyncio.run(scenario())


def test_observer_errors_are_diagnostics_and_cancel_propagates(api):
    async def scenario():
        runtime = api.ExtensionRuntime()

        def bad(e, c):
            raise ValueError("observer")

        await runtime.register(lambda ext: ext.on("agent_start", bad))
        session = api.AgentSession(
            model=ScriptedModel(api.AssistantMessage((), "stop")), extensions=runtime
        )
        await session.prompt("hello")
        assert runtime.diagnostics[0]["event"] == "agent_start"
        runtime.close()
        runtime = api.ExtensionRuntime()

        async def cancel(e, c):
            raise asyncio.CancelledError()

        await runtime.register(lambda ext: ext.on("input", cancel))
        session = api.AgentSession(model=ScriptedModel(), extensions=runtime)
        with pytest.raises(asyncio.CancelledError):
            await session.prompt("cancel")
        assert not session.busy and session.messages == []
        runtime.close()

    asyncio.run(scenario())


def test_closed_runtime_does_not_invoke_escaped_hooks(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        effect = AsyncMock()
        await runtime.register(lambda ext: ext.on("input", effect))
        session = api.AgentSession(model=ScriptedModel(), extensions=runtime)
        runtime.close()
        with pytest.raises(RuntimeError, match="关闭"):
            await session.prompt("late")
        effect.assert_not_awaited()
        assert not session.busy and session.messages == []

    asyncio.run(scenario())
