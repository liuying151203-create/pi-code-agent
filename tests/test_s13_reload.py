"""s13：动态工具真实调用、代次失效、重载回滚与模块源码新鲜度。"""

import asyncio
import os
import py_compile
import sys
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(params=["pi_code_agent", "tutorials.s13_reload"])
def api(request):
    return load_runtime(request.param)


def test_dynamic_active_tools_refresh_next_request_and_preserve_batch(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        escaped = []
        effects = []

        def setup(ext):
            escaped.append(ext)

            async def switch(args):
                ext.set_active_tools(["after", "unknown", "after"])
                return api.ToolResult("switched")

            async def second(args):
                effects.append("second")
                return api.ToolResult("second")

            ext.register_tool(api.Tool("switch", "switch", switch))
            ext.register_tool(api.Tool("second", "second", second))

        await runtime.register(setup)
        model = ScriptedModel(
            api.AssistantMessage(
                (api.ToolCall("1", "switch", {}), api.ToolCall("2", "second", {})), "tool_use"
            ),
            api.AssistantMessage((api.ToolCall("3", "after", {}),), "tool_use"),
            api.AssistantMessage((), "stop"),
        )
        session = api.AgentSession(model=model, extensions=runtime)
        escaped[0].register_tool(
            api.Tool("after", "after", AsyncMock(return_value=api.ToolResult("after")))
        )
        session.set_active_tools(["switch", "second"])
        await session.prompt("go")
        assert [[tool.name for tool in call["tools"]] for call in model.calls] == [
            ["switch", "second"],
            ["after"],
            ["after"],
        ]
        assert effects == ["second"] and session.messages[-2].content == "after"
        await session.aclose()

    asyncio.run(scenario())


def test_reload_preserves_history_flags_and_invalidates_escaped_context(api, tmp_path):
    async def scenario():
        runtime = api.ExtensionRuntime()
        contexts = []
        lifecycle = []
        escaped = []

        def setup(ext):
            escaped.append(ext)
            ext.register_flag("name", type="string", default="default")

            def start(e, c):
                contexts.append(c)
                lifecycle.append((e["type"], e["reason"]))

            ext.on("session_start", start)
            ext.on("session_shutdown", lambda e, c: lifecycle.append((e["type"], e["reason"])))

        await runtime.register(setup)
        runtime.parse_flags(["--name=saved"])
        session = api.AgentSession(
            model=ScriptedModel(api.AssistantMessage((), "stop")), extensions=runtime
        )
        await session.prompt("start")
        history = session.messages
        old_context = contexts[0]
        old_notify = old_context.ui.notify
        await session.reload()
        assert session.messages == history and session.extensions is not runtime
        assert escaped[-1].get_flag("name") == "saved"
        assert lifecycle == [
            ("session_start", "startup"),
            ("session_shutdown", "reload"),
            ("session_start", "reload"),
        ]
        with pytest.raises(RuntimeError):
            old_context.append_entry("stale", {})
        with pytest.raises(RuntimeError):
            old_notify("late")
        with pytest.raises(RuntimeError):
            escaped[0].set_active_tools([])
        await session.aclose()
        await session.aclose()
        assert lifecycle[-1] == ("session_shutdown", "shutdown")

    asyncio.run(scenario())


def plugin_source():
    return 'from .helper import VALUE\ndef setup(api):\n    async def execute(args): return api.ToolResult(VALUE)\n    api.register_tool(api.Tool("hello","hello",execute))\n'


def test_reload_refreshes_same_size_same_timestamp_helper_and_cleans_modules(api, tmp_path):
    async def scenario():
        helper = tmp_path / "helper.py"
        helper.write_text('VALUE="old"', encoding="utf-8")
        stat = helper.stat()
        py_compile.compile(str(helper), doraise=True)
        entry = tmp_path / "index.py"
        entry.write_text(plugin_source(), encoding="utf-8")
        runtime = api.ExtensionRuntime()
        await runtime.load([entry])
        session = api.AgentSession(model=ScriptedModel(), extensions=runtime)
        old_tool = session.tools[0]
        old_prefixes = tuple(runtime._namespaces)
        assert (await old_tool.execute({})).content == "old"
        helper.write_text('VALUE="new"', encoding="utf-8")
        os.utime(helper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        await session.reload()
        assert (await session.tools[0].execute({})).content == "new"
        with pytest.raises(RuntimeError):
            await old_tool.execute({})
        assert not any(name.startswith(old_prefixes) for name in sys.modules)
        await session.aclose()

    asyncio.run(scenario())


def test_failed_reload_keeps_old_runtime_tools_and_history(api, tmp_path):
    async def scenario():
        helper = tmp_path / "helper.py"
        helper.write_text('VALUE="old"', encoding="utf-8")
        entry = tmp_path / "index.py"
        entry.write_text(plugin_source(), encoding="utf-8")
        runtime = api.ExtensionRuntime()
        await runtime.load([entry])
        session = api.AgentSession(
            model=ScriptedModel(api.AssistantMessage((), "stop")), extensions=runtime
        )
        await session.prompt("initial")
        history = session.messages
        entry.write_text('def setup(api): raise RuntimeError("broken reload")', encoding="utf-8")
        with pytest.raises(ValueError, match="broken reload"):
            await session.reload()
        assert session.extensions is runtime and not session.busy and session.messages == history
        assert (await session.tools[0].execute({})).content == "old"
        await session.aclose()

    asyncio.run(scenario())


def test_resources_reload_is_atomic_and_refreshes_actual_prompt(api, tmp_path):
    async def scenario():
        agents = tmp_path / "AGENTS.md"
        agents.write_text("old policy", encoding="utf-8")
        directory = tmp_path / ".pi-code-agent/prompts"
        directory.mkdir(parents=True)
        template = directory / "note.md"
        template.write_text("old $1", encoding="utf-8")
        resources = api.ResourceLoader(tmp_path).load()
        model = ScriptedModel(api.AssistantMessage((), "stop"), api.AssistantMessage((), "stop"))
        session = api.AgentSession(model=model, resources=resources)
        await session.prompt("/note input")
        agents.write_text("new policy", encoding="utf-8")
        template.write_text("new $1", encoding="utf-8")
        await session.reload()
        await session.prompt("/note input")
        assert (
            model.calls[-1]["messages"][-1].content == "new input"
            and "new policy" in model.calls[-1]["system"]
        )
        previous = session.resources
        template.write_text("---\nbad: [\n---\ninvalid", encoding="utf-8")
        with pytest.raises(ValueError):
            await session.reload()
        assert session.resources is previous and not session.busy
        await session.aclose()

    asyncio.run(scenario())


def test_reload_while_busy_rejects_without_interrupting_run(api):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Model:
            async def complete(self, **kwargs):
                entered.set()
                await release.wait()
                return api.AssistantMessage((), "stop")

        session = api.AgentSession(model=Model())
        task = asyncio.create_task(session.prompt("start"))
        await entered.wait()
        with pytest.raises(RuntimeError):
            await session.reload()
        assert session.busy
        release.set()
        await task
        await session.aclose()

    asyncio.run(scenario())


def test_discovery_runs_again_for_new_extensions(api, tmp_path):
    async def scenario():
        runtime = api.ExtensionRuntime()
        runtime.discover = lambda: api.discover_extensions(
            tmp_path, paths=[tmp_path], project=False
        )
        await runtime.load(runtime.discover())
        session = api.AgentSession(model=ScriptedModel(), extensions=runtime)
        (tmp_path / "new.py").write_text(
            'def setup(api):\n    async def execute(args): return api.ToolResult("new")\n    api.register_tool(api.Tool("new","new",execute))',
            encoding="utf-8",
        )
        await session.reload()
        assert session.get_active_tools() == ["new"]
        await session.aclose()

    asyncio.run(scenario())


def test_shutdown_holds_busy_barrier_and_closed_session_rejects_delivery(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        entered, release = asyncio.Event(), asyncio.Event()

        async def shutdown(e, c):
            entered.set()
            await release.wait()

        await runtime.register(lambda ext: ext.on("session_shutdown", shutdown))
        session = api.AgentSession(
            model=ScriptedModel(api.AssistantMessage((), "stop")), extensions=runtime
        )
        await session.start()
        closing = asyncio.create_task(session.aclose())
        await entered.wait()
        try:
            assert session.busy
            with pytest.raises(RuntimeError):
                await session.prompt("during shutdown")
        finally:
            release.set()
            await closing
        with pytest.raises(RuntimeError):
            await session.send_message("after shutdown")
        assert session.messages == []

    asyncio.run(scenario())


def test_cli_releases_owned_model_when_shutdown_handler_cancels(api, tmp_path, monkeypatch):
    from types import SimpleNamespace

    model = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(api.cli, "load_model_config", lambda *a: SimpleNamespace(max_tokens=1))
    monkeypatch.setattr(api.cli, "create_model", lambda *a: model)
    plugin = tmp_path / "cancel.py"
    plugin.write_text(
        'import asyncio\ndef setup(api):\n    async def shutdown(e,c): raise asyncio.CancelledError()\n    api.on("session_shutdown",shutdown)\n',
        encoding="utf-8",
    )
    with pytest.raises(asyncio.CancelledError):
        api.cli.main(
            [
                "--no-session",
                "--no-resources",
                "--no-extensions",
                "-e",
                str(plugin),
                "--workspace",
                str(tmp_path),
                "--prompt",
                "/help",
            ],
            loop=api.agent_loop,
        )
    model.aclose.assert_awaited_once()


def test_reload_keeps_inline_before_file_registration_priority(api, tmp_path):
    async def scenario():
        runtime = api.ExtensionRuntime()

        def inline(ext):
            ext.register_tool(
                api.Tool("same", "inline", AsyncMock(return_value=api.ToolResult("inline")))
            )

        await runtime.register(inline, source="sdk-inline")
        plugin = tmp_path / "file.py"
        plugin.write_text(
            'def setup(api):\n    async def execute(args): return api.ToolResult("file")\n    api.register_tool(api.Tool("same","file",execute))\n',
            encoding="utf-8",
        )
        await runtime.load([plugin])
        session = api.AgentSession(model=ScriptedModel(), extensions=runtime)
        assert session.tools[0].description == "inline"
        await session.reload()
        assert session.tools[0].description == "inline"
        await session.aclose()

    asyncio.run(scenario())
