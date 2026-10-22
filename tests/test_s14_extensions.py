"""s14 回归：注册、Hook、事件、权限及完整交互入口。"""

import json
import sys
from types import ModuleType

import pytest

import tutorials.s14_extensions.code as chapter
from tutorials.s14_extensions.extensions import read_preview
from tutorials.s14_extensions.registry import ExtensionAPI, load_extensions


def make_api(*, dispatch=None, permission=None, tools=None):
    """用不触碰真实文件的后端创建注册层。"""
    return ExtensionAPI(
        base_tools=chapter.TOOLS if tools is None else tools,
        base_dispatch=dispatch if dispatch is not None else lambda name, args: "基础结果",
        permission_hook=permission if permission is not None else lambda name, args: None,
    )


def tool_definition(name="example_tool"):
    return {"name": name, "description": "测试工具", "input_schema": {"type": "object"}}


def execute(api, name, arguments):
    """和真实循环一样，先运行扩展 Hooks，再进入注册层分发。"""
    return chapter.execute_tool(name, arguments, dispatch=api.dispatch, hooks=api.hooks)


def test_register_keeps_model_catalog_and_handler_together_without_changing_global_tools():
    before = json.dumps(chapter.TOOLS)
    definition = tool_definition()
    api = make_api()
    api.register_tool(definition, lambda args: f"输入={args['value']}")
    definition["description"] = "外部修改"
    assert api.tools[-1]["description"] == "测试工具"
    assert api.dispatch("example_tool", {"value": 3}) == "输入=3"
    assert json.dumps(chapter.TOOLS) == before


@pytest.mark.parametrize("name", ["read_file", "bash", "example_tool"])
def test_duplicate_and_hidden_builtin_names_cannot_be_overridden(name):
    api = make_api(tools=chapter.permissions.READ_ONLY_TOOLS)
    api.register_tool(tool_definition(), lambda args: "结果")
    with pytest.raises(ValueError, match="重复|内置"):
        api.register_tool(tool_definition(name), lambda args: "覆盖")


@pytest.mark.parametrize(
    "definition",
    [
        None,
        {},
        tool_definition("bad-name"),
        {"name": "demo", "description": "", "input_schema": {"type": "object"}},
        {"name": "demo", "description": "演示", "input_schema": {"type": "array"}},
    ],
)
def test_invalid_registration_changes_neither_catalog_nor_handlers(definition):
    api = make_api()
    size = len(api.tools)
    with pytest.raises((TypeError, ValueError)):
        api.register_tool(definition, lambda args: "结果")
    assert len(api.tools) == size
    assert api.handlers == {}


def test_before_hook_blocks_and_after_hooks_process_result_in_registration_order():
    calls = []
    api = make_api(dispatch=lambda name, args: calls.append(name) or "后端结果")
    api.before_tool_call(lambda name, args: "Error: 阻断")
    api.after_tool_call(lambda name, args, result: result + " → A")
    api.after_tool_call(lambda name, args, result: result + " → B")
    assert execute(api, "read_file", {"path": "README.md"}) == "Error: 阻断 → A → B"
    assert calls == []


def test_event_subscription_filters_and_orders_observers():
    calls = []
    api = make_api()
    api.on("tool_result", lambda event: calls.append(("A", event.type)))
    api.on("tool_result", lambda event: calls.append(("B", event.type)))
    events = chapter.EventDispatcher()
    events.subscribe(api.handle_event)
    events.emit(chapter.AgentEvent(type="agent_start", data={}))
    events.emit(chapter.AgentEvent(type="tool_result", data={}))
    assert calls == [("A", "tool_result"), ("B", "tool_result")]
    with pytest.raises(ValueError, match="未知事件"):
        api.on("imaginary_event", lambda event: None)


def test_default_module_registers_tool_hook_and_observer():
    api = make_api()
    assert load_extensions(api, [chapter.DEFAULT_EXTENSION]) == (chapter.DEFAULT_EXTENSION,)
    assert set(api.handlers) == {"read_preview"}
    assert len(api.hooks.before_tool_call) == 1
    assert len(api.listeners["tool_result"]) == 1


def test_preview_delegates_real_read_parameters_to_permission_and_backend():
    seen = []
    api = make_api(
        permission=lambda name, args: seen.append(("权限", name, dict(args))) or None,
        dispatch=lambda name, args: seen.append(("后端", name, dict(args))) or "源码证据",
    )
    read_preview.register(api)
    assert execute(api, "read_preview", {"path": "demo.py"}) == "源码证据"
    assert seen == [
        ("权限", "read_file", {"path": "demo.py", "start_line": 1, "limit": 20}),
        ("后端", "read_file", {"path": "demo.py", "start_line": 1, "limit": 20}),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "demo.py", "limit": 81},
        {"path": "demo.py", "limit": True},
        {"path": "demo.py", "start_line": 0},
        {"path": "demo.py", "limit": "20"},
    ],
)
def test_preview_invalid_range_is_blocked_without_backend(arguments):
    api = make_api(dispatch=lambda *args: pytest.fail("Hook 应先阻断"))
    read_preview.register(api)
    assert execute(api, "read_preview", arguments).startswith("Error:")


@pytest.mark.parametrize("arguments", [{}, {"path": 1}, {"path": "a", "command": "x"}])
def test_preview_invalid_arguments_return_tool_error(arguments):
    api = make_api(dispatch=lambda *args: pytest.fail("参数无效不应执行"))
    read_preview.register(api)
    assert execute(api, "read_preview", arguments).startswith("Error:")


def test_read_only_cannot_be_bypassed_by_an_extension_tool_name(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    permission = chapter.make_permission_hook(
        permission=chapter.permissions.check_read_only_permission,
        confirm=lambda *args: pytest.fail("只读模式不审批"),
    )
    api = make_api(
        tools=chapter.permissions.READ_ONLY_TOOLS,
        permission=permission,
        dispatch=lambda *args: pytest.fail("被拒绝的操作不能到达后端"),
    )
    read_preview.register(api)
    assert "Permission denied:" in execute(api, "read_preview", {"path": ".env"})
    # 包装工具不能通过改名，把 write_file 委托伪装成只读操作。
    api.register_tool(
        tool_definition("wrapped_write"),
        lambda args: api.call_tool("write_file", {"path": "a.py", "content": "x"}),
    )
    assert "Permission denied:" in execute(api, "wrapped_write", {})
    assert "Permission denied:" in execute(api, "bash", {"command": "echo x"})
    assert list(tmp_path.iterdir()) == []


def test_preview_uses_real_file_tool_and_preserves_read_window(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    source = tmp_path / "demo.py"
    source.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    permission = chapter.make_permission_hook(
        permission=chapter.permissions.check_read_only_permission,
        confirm=lambda *args: pytest.fail("只读不请求确认"),
    )
    api = make_api(
        tools=chapter.permissions.READ_ONLY_TOOLS,
        permission=permission,
        dispatch=chapter.dispatch_tool,
    )
    read_preview.register(api)
    result = execute(api, "read_preview", {"path": "demo.py", "start_line": 2, "limit": 1})
    assert "第二行" in result
    assert "第一行" not in result
    assert "第三行" not in result
    assert source.read_text(encoding="utf-8") == "第一行\n第二行\n第三行\n"


def test_wrapped_write_in_default_mode_still_requires_base_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    confirmations = []
    permission = chapter.make_permission_hook(
        permission=chapter.permissions.check_permission,
        confirm=lambda name, args, reason: confirmations.append(name) or False,
    )
    api = make_api(permission=permission, dispatch=lambda *args: pytest.fail("取消审批不能写入"))
    api.register_tool(
        tool_definition("wrapped_write"),
        lambda args: api.call_tool("write_file", {"path": "a.py", "content": "x"}),
    )
    assert "Permission denied:" in execute(api, "wrapped_write", {})
    assert confirmations == ["write_file"]
    assert list(tmp_path.iterdir()) == []


def test_extension_event_failure_is_not_silently_discarded():
    api = make_api()

    def broken_listener(event):
        raise RuntimeError("观察者故障")

    api.on("tool_result", broken_listener)
    with pytest.raises(RuntimeError, match="观察者故障"):
        api.handle_event(chapter.AgentEvent(type="tool_result", data={}))


def test_unknown_tool_and_extension_recursive_delegation_do_not_reach_backend():
    api = make_api(dispatch=lambda *args: pytest.fail("未注册工具不能执行"))
    assert api.dispatch("unknown", {}).startswith("Error:")
    api.register_tool(tool_definition(), lambda args: api.call_tool("example_tool", args))
    assert api.dispatch("example_tool", {}).startswith("Error:")


def test_handler_failure_and_non_string_result_become_error_text():
    api = make_api()

    def broken(arguments):
        raise RuntimeError("扩展内部异常")

    api.register_tool(tool_definition("broken"), broken)
    api.register_tool(tool_definition("wrong_result"), lambda args: 1)
    assert "Error:" in api.dispatch("broken", {})
    assert "Error:" in api.dispatch("wrong_result", {})


@pytest.mark.parametrize("names", [["bad/path.py"], ["x", "x"], [".hidden"]])
def test_invalid_module_choices_are_rejected_before_import(names, monkeypatch):
    monkeypatch.setattr(
        "tutorials.s14_extensions.registry.importlib.import_module", lambda *args: pytest.fail()
    )
    with pytest.raises(ValueError):
        load_extensions(make_api(), names)


def test_missing_register_and_registration_failure_stop_loading(monkeypatch):
    empty = ModuleType("test_empty_extension")
    broken = ModuleType("test_broken_extension")

    def register(api):
        raise ValueError("注册失败")

    broken.register = register
    monkeypatch.setitem(sys.modules, empty.__name__, empty)
    monkeypatch.setitem(sys.modules, broken.__name__, broken)
    with pytest.raises(RuntimeError, match="register"):
        load_extensions(make_api(), [empty.__name__])
    with pytest.raises(RuntimeError, match="注册失败"):
        load_extensions(make_api(), [broken.__name__])
    with pytest.raises(RuntimeError, match="加载失败"):
        load_extensions(make_api(), ["no_such_extension_for_s14"])


def test_loop_is_local_and_preserves_fatal_backend_batch_pairing():
    assert chapter.agent_loop.__module__ == chapter.__name__
    calls = []
    events = []
    api = make_api(dispatch=lambda name, args: "unused")

    def fatal(arguments):
        calls.append("fatal")
        raise chapter.SandboxError("后端断开")

    api.register_tool(tool_definition("fatal"), fatal)
    api.register_tool(tool_definition("not_run"), lambda args: calls.append("不应执行") or "x")
    response = chapter.ModelResponse(
        content=[
            {"type": "tool_use", "id": name, "name": name, "input": {}}
            for name in ("fatal", "not_run")
        ],
        model="offline",
        stop_reason="tool_use",
        usage=None,
    )
    messages = []
    with pytest.raises(chapter.SandboxError):
        chapter.agent_loop(
            messages,
            create_message=lambda **kwargs: response,
            dispatch=api.dispatch,
            system="测试",
            hooks=api.hooks,
            tools=api.tools,
            emit=events.append,
        )
    assert calls == ["fatal"]
    assert [item["tool_use_id"] for item in messages[-1]["content"]] == ["fatal", "not_run"]
    assert events[-1].type == "agent_end"


def test_main_runs_extension_saves_evidence_and_restores_usage(tmp_path, monkeypatch, capsys):
    calls = []
    requests = []

    class Provider:
        model = "offline-s14"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            assert "read_preview" in {tool["name"] for tool in kwargs["tools"]}
            assert "bash" not in {tool["name"] for tool in kwargs["tools"]}
            if len(requests) % 2:
                content = [
                    {
                        "type": "tool_use",
                        "id": f"preview-{len(requests)}",
                        "name": "read_preview",
                        "input": {"path": "README.md"},
                    }
                ]
                stop_reason = "tool_use"
            else:
                assert kwargs["messages"][-1]["content"][0]["content"] == "离线源码证据"
                content = [{"type": "text", "text": "完成预览"}]
                stop_reason = "end_turn"
            return chapter.ModelResponse(
                content=content,
                model=self.model,
                stop_reason=stop_reason,
                usage=chapter.runtime.ModelUsage(input_tokens=10, output_tokens=2),
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces")
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setattr(
        chapter, "dispatch_tool", lambda name, args: calls.append(name) or "离线源码证据"
    )
    inputs = iter(["用 read_preview 预览 README.md", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    chapter.main(["--mode", "read-only"])
    session_path = next((tmp_path / "sessions").glob("*.jsonl"))
    session = chapter.SessionManager.open(session_path)
    assert session.session_stats().known_tokens == 24
    assert session.session_stats().tool_calls == 1
    assert calls == ["read_file"]
    assert "扩展  read_preview 完成" in capsys.readouterr().out
    records = [
        json.loads(line)
        for path in (tmp_path / "traces").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["type"] == "tool_result" for record in records)

    inputs = iter(["继续预览", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    chapter.main(["--mode", "read-only", "--session", str(session_path)])
    restored = chapter.SessionManager.open(session_path)
    assert restored.session_stats().known_tokens == 48
    assert restored.session_stats().tool_calls == 2
    assert len(requests[2]["messages"]) >= 5


def test_help_does_not_load_modules_or_create_runtime_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter, "load_extensions", lambda *args: pytest.fail("help 不加载扩展"))
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0
    assert list(tmp_path.iterdir()) == []


def test_failed_registration_does_not_create_provider_or_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chapter, "create_model_provider", lambda **kwargs: pytest.fail("注册失败不初始化")
    )
    with pytest.raises(RuntimeError, match="加载失败"):
        chapter.main(["--extension", "no_such_extension_for_s14"])
    assert list(tmp_path.iterdir()) == []
