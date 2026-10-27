"""s09：命令四种效果独立，参数事务与真实 CLI。"""

import asyncio

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(params=["pi_code_agent", "tutorials.s09_commands"])
def api(request):
    return load_runtime(request.param)


def test_commands_separate_ui_state_model_and_trigger(api, tmp_path):
    async def scenario():
        runtime = api.ExtensionRuntime()
        visible = []

        async def command(args, ctx):
            ctx.ui.notify("notice:" + args)
            ctx.append_entry("counter", {"count": 1})
            await ctx.send_message("model context", display=False)

        await runtime.register(
            lambda ext: ext.register_command("save", command, description="save state")
        )
        store = api.SessionStore.create(tmp_path / "state.jsonl", tmp_path)
        model = ScriptedModel(api.AssistantMessage((api.Text("done"),), "stop"))
        session = api.AgentSession(
            model=model, store=store, extensions=runtime, notify=visible.append
        )
        assert await session.run_command("/save raw args")
        assert visible == ["notice:raw args"] and model.calls == []
        assert len(session.messages) == 1 and not session.messages[0].display
        reopened = api.SessionStore.open(store.path, tmp_path)
        assert reopened.entries[0]["type"] == "custom" and len(reopened.messages) == 1
        assert not await session.run_command("/unknown")
        await session.send_message("start", trigger_turn=True)
        assert len(model.calls) == 1
        assert [m.content for m in model.calls[0]["messages"]] == ["model context", "start"]
        assert all(m.role == "user" for m in model.calls[0]["messages"])
        runtime.close()

    asyncio.run(scenario())


def test_flags_are_strict_and_factory_defaults_are_transactional(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        escaped = []

        def setup(ext):
            escaped.append(ext)
            ext.register_flag("enabled", default=False)
            ext.register_flag("label", type="string", default="old")

        await runtime.register(setup)
        assert escaped[0].get_flag("enabled") is False
        runtime.parse_flags(["--enabled", "--label=new"])
        assert escaped[0].get_flag("enabled") is True
        assert escaped[0].get_flag("label") == "new"
        for args in [["--unknown"], ["--label"], ["--enabled=maybe"]]:
            with pytest.raises(ValueError):
                runtime.parse_flags(args)
        assert escaped[0].get_flag("label") == "new"
        runtime.close()

    asyncio.run(scenario())


def test_help_does_not_enter_model_and_reserved_commands_fail(api):
    async def scenario():
        runtime = api.ExtensionRuntime()
        with pytest.raises(ValueError):
            await runtime.register(lambda ext: ext.register_command("help", lambda a, c: None))
        model = ScriptedModel()
        notices = []
        session = api.AgentSession(model=model, extensions=runtime, notify=notices.append)
        assert await session.run_command("/help")
        assert "/help" in notices[0] and model.calls == [] and session.messages == []
        runtime.close()

    asyncio.run(scenario())


def test_real_cli_extension_flags_and_command(api, tmp_path, capsys):
    plugin = tmp_path / "plugin.py"
    plugin.write_text(
        'def setup(api):\n    api.register_flag("label", type="string", default="old")\n    api.register_command("show", lambda args, ctx: ctx.ui.notify(api.get_flag("label")))\n',
        encoding="utf-8",
    )
    cli = api.cli
    assert (
        cli.main(
            [
                "--demo",
                "--no-session",
                "--no-extensions",
                "-e",
                str(plugin),
                "--workspace",
                str(tmp_path),
                "--label=actual",
                "--prompt",
                "/show",
            ],
            loop=api.agent_loop,
        )
        == 0
    )
    assert "actual" in capsys.readouterr().out


def test_message_only_delivery_is_busy_until_observers_finish(api):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        session = api.AgentSession(model=ScriptedModel(api.AssistantMessage((), "stop")))

        async def observer(event):
            if event["type"] == "message_start":
                entered.set()
                await release.wait()

        session.subscribe(observer)
        sending = asyncio.create_task(session.send_message("context"))
        await entered.wait()
        try:
            assert session.busy
            with pytest.raises(RuntimeError):
                await session.prompt("interleave")
        finally:
            release.set()
            await sending
        assert not session.busy

    asyncio.run(scenario())
