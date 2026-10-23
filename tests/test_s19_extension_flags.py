"""s19：扩展只声明一次、启动参数真实接线及创建资源前的 CLI 校验。"""

import ast
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from test_s18_commands import Provider, answer

import tutorials.s18_commands.code as previous
import tutorials.s19_extension_flags.code as chapter
from pi_code_agent import cli, create_agent
from pi_code_agent.extensions.todo import register as register_todo
from pi_code_agent.flags import FlagRegistry
from pi_code_agent.registry import ExtensionAPI


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_sdk_values_reach_real_todo_handler_and_progress(factory, tmp_path, capsys):
    workspace = tmp_path / "target"
    workspace.mkdir()
    call = {
        "type": "tool_use",
        "id": "t1",
        "name": "todo",
        "input": {"action": "add", "text": "验证扩展参数"},
    }
    provider = Provider(answer([call], "tool_use"), answer())
    agent = factory(
        provider=provider,
        workspace=workspace,
        data_dir=tmp_path / "records",
        flag_values={"todo-heading": "学习清单", "todo-progress": False},
    )
    agent.register_extension(register_todo)
    events = []
    agent.subscribe(events.append)
    agent.execute_command("/todos")
    assert any("学习清单：0/0" in event.data.get("message", "") for event in events)
    agent.run("添加任务")
    assert "进度" not in capsys.readouterr().out
    assert agent.session.latest_tool_details("todo")["todos"][0]["text"] == "验证扩展参数"
    assert agent.extensions.get_flag("todo-heading") == "学习清单"
    assert [event.type for event in events].count("session_start") == 1
    assert len(provider.calls) == 2
    agent.close()


@pytest.mark.parametrize("entry", [cli, chapter])
def test_cli_and_tutorial_help_include_flags_without_configuration_or_records(
    entry, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        entry, "load_environment", Mock(side_effect=AssertionError("帮助不能读模型配置"))
    )
    with pytest.raises(SystemExit) as error:
        entry.main(["--extension", "pi_code_agent.extensions.todo", "--help"])
    assert error.value.code == 0
    text = capsys.readouterr().out
    assert "--todo-heading VALUE" in text and "--no-todo-progress" in text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [cli, chapter])
def test_real_entry_applies_flags_and_imports_extension_only_once(entry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "MODEL_PROVIDER": "anthropic",
            "MODEL_ID": "offline",
            "ANTHROPIC_API_KEY": "offline",
            "MODEL_CONTEXT_WINDOW_TOKENS": "128000",
        },
    )
    provider = Provider(answer())
    monkeypatch.setattr(entry, "create_model_provider", Mock(return_value=provider))
    module = ModuleType("s19_test_extension")
    starts, registrations, seen = [], [], []

    def register(api):
        registrations.append("register")
        api.register_flag("label", type="string", default="default", description="标签")
        api.register_flag("share", type="boolean", default=True, description="发送模型消息")
        assert not api.is_bound

        def on_start(event):
            starts.append((api.get_flag("label"), api.get_flag("share")))
            assert api.is_bound and api.session.path.is_file()

        def show(args, ctx):
            seen.append((args, api.get_flag("label"), api.get_flag("share")))
            if api.get_flag("share"):
                api.send_message(str(api.get_flag("label")), display=False)
            ctx.ui.notify(str(api.get_flag("label")))

        api.on("session_start", on_start)
        api.register_command("show", show, description="展示")

    module.register = register
    monkeypatch.setitem(sys.modules, module.__name__, module)
    inputs = iter(["/show 带空格的 参数", "继续", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    target = tmp_path / "target"
    target.mkdir()
    entry.main(
        [
            "--workspace",
            str(target),
            "--data-dir",
            str(tmp_path / "records"),
            "--extension",
            module.__name__,
            "--label=实际值",
            "--no-share",
        ]
    )
    assert registrations == ["register"]
    assert starts == [("实际值", False)]
    assert seen == [("带空格的 参数", "实际值", False)]
    assert provider.calls[0]["messages"] == [{"role": "user", "content": "继续"}]


@pytest.mark.parametrize("entry", [cli, chapter])
@pytest.mark.parametrize("extra", [["--todo-heading"], ["--typo"], ["--todo-progress=false"]])
def test_invalid_flags_precede_client_config_and_session(entry, extra, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    load = Mock(side_effect=AssertionError("参数错误不能读模型配置"))
    monkeypatch.setattr(entry, "load_environment", load)
    with pytest.raises(SystemExit) as error:
        entry.main(["--extension", "pi_code_agent.extensions.todo", *extra])
    assert error.value.code == 2
    load.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "name,type,default",
    [
        ("workspace", "string", "x"),
        ("/bad", "string", "x"),
        ("bad", "integer", 1),
        ("flag", "boolean", "false"),
        ("label", "string", False),
    ],
)
def test_invalid_declarations_are_rejected(name, type, default):
    with pytest.raises(ValueError):
        FlagRegistry().register(name, type=type, default=default, description="说明")


def test_defaults_overrides_collision_and_freeze_are_explicit():
    flags = FlagRegistry()
    flags.set_values({"label": "CLI 值", "enabled": False})
    flags.register("label", type="string", default="默认", description="标签")
    flags.register("enabled", type="boolean", default=True, description="开关")
    flags.validate()
    assert flags.get("label") == "CLI 值" and flags.get("enabled") is False
    assert flags.get("missing") is None
    with pytest.raises(ValueError, match="重复"):
        flags.register("label", type="string", description="重复")
    with pytest.raises(ValueError, match="冲突"):
        flags.register("no-enabled", type="string", description="反向冲突")
    with pytest.raises(ValueError, match="类型"):
        flags.set_values({"enabled": "false"})
    assert flags.get("enabled") is False
    flags.frozen = True
    with pytest.raises(RuntimeError, match="固定"):
        flags.register("late", type="boolean", description="晚注册")
    with pytest.raises(RuntimeError, match="固定"):
        flags.set_values({})


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_preloaded_container_binds_once_and_restores_todo(factory, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    api = ExtensionAPI()
    register_todo(api)
    api.flags.set_values({"todo-heading": "恢复清单"})
    with pytest.raises(RuntimeError, match="未绑定"):
        api.get_tool_details("todo")
    with pytest.raises(RuntimeError, match="未绑定"):
        api.call_tool("read_file", {"path": "x"})
    with pytest.raises(RuntimeError, match="未绑定"):
        api.send_message("不能发送")
    provider = Provider(answer())
    agent = factory(
        provider=provider, workspace=target, data_dir=tmp_path / "records", extensions=api
    )
    agent.initialize_extensions()
    with pytest.raises(ValueError, match="已经绑定"):
        factory(provider=provider, workspace=target, data_dir=tmp_path / "other", extensions=api)
    assert not (tmp_path / "other").exists()
    assert agent.extensions is api and api.get_flag("todo-heading") == "恢复清单"
    assert {tool["name"] for tool in api.tools} == {"read_file", "grep", "glob", "todo"}
    agent.close()


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_unknown_sdk_values_fail_before_first_task_is_saved(factory, tmp_path):
    agent = factory(
        provider=Provider(),
        workspace=tmp_path,
        data_dir=tmp_path / "records",
        flag_values={"typo": "bad"},
    )
    with pytest.raises(ValueError, match="未知扩展参数"):
        agent.run("不能保存")
    assert agent.session.load_entries() == []
    agent.close()


@pytest.mark.parametrize("entry", [cli, chapter])
def test_bad_extension_declaration_stops_before_configuration(entry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = ModuleType("s19_bad_extension")

    def register(api):
        api.register_flag("mode", type="string", description="不能覆盖内置参数")

    module.register = register
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(entry, "load_environment", Mock(side_effect=AssertionError("不能读取配置")))
    with pytest.raises(RuntimeError, match="扩展加载失败"):
        entry.main(["--extension", module.__name__])
    assert list(tmp_path.iterdir()) == []


def test_tutorial_keeps_full_core_loop():
    def loop(module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "agent_loop"
        )

    assert ast.dump(loop(chapter)) == ast.dump(loop(previous))


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_interrupted_startup_cannot_skip_remaining_initialization(factory, tmp_path):
    """初始化中断后不能把 started 标记当作已经完成。"""
    agent = factory(provider=Provider(answer()), workspace=tmp_path, data_dir=tmp_path / "records")

    def interrupt(event):
        if event.type == "session_start":
            raise KeyboardInterrupt

    agent.subscribe(interrupt)
    with pytest.raises(KeyboardInterrupt):
        agent.initialize_extensions()
    with pytest.raises(RuntimeError, match="已关闭"):
        agent.run("不能跳过未完成的初始化")
    assert agent.session.load_entries() == []
    agent.close()


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_predeclared_todo_recovers_persisted_state_on_start(factory, tmp_path):
    workspace = tmp_path / "target"
    workspace.mkdir()
    (workspace / "note.txt").write_text("绑定后读取真实基础工具", encoding="utf-8")
    call = {
        "type": "tool_use",
        "id": "t1",
        "name": "todo",
        "input": {"action": "add", "text": "重启后仍可查看"},
    }
    first = factory(
        provider=Provider(answer([call], "tool_use"), answer()),
        workspace=workspace,
        data_dir=tmp_path / "records",
    )
    first.register_extension(register_todo)
    first.run("保存待办")
    path = first.session.path
    state = first.session.latest_tool_details("todo")
    first.close()
    api = ExtensionAPI()
    register_todo(api)
    second = factory(
        provider=Provider(),
        workspace=workspace,
        data_dir=tmp_path / "records",
        session_path=path,
        extensions=api,
        flag_values={"todo-heading": "恢复标题"},
    )
    events = []
    second.subscribe(events.append)
    second.execute_command("/todos")
    text = "\n".join(event.data.get("message", "") for event in events)
    assert "恢复标题：0/1" in text and "重启后仍可查看" in text
    assert second.session.latest_tool_details("todo") == state
    assert "绑定后读取真实基础工具" in api.call_tool("read_file", {"path": "note.txt"})
    assert api.call_tool("bash", {"command": "echo should-not-run"}).startswith(
        "Permission denied:"
    )
    second.close()


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
def test_startup_can_save_state_send_messages_and_request_one_turn(factory, tmp_path):
    api = ExtensionAPI()
    order = []
    provider = Provider(answer())

    def start(event):
        api.append_entry("startup", {"ready": True})
        api.send_message("启动事实", display=False, trigger_turn=True)
        api.send_user_message("检查启动状态")
        assert provider.calls == []
        order.append("initialized")

    api.on("session_start", start)
    agent = factory(
        provider=provider, workspace=tmp_path, data_dir=tmp_path / "records", extensions=api
    )
    agent.initialize_extensions()
    assert order == ["initialized"] and len(provider.calls) == 1
    assert [message["content"] for message in provider.calls[0]["messages"]] == [
        "启动事实",
        "检查启动状态",
    ]
    agent.initialize_extensions()
    assert len(provider.calls) == 1
    agent.close()
