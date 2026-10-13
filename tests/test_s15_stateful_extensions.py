"""s15 回归：结构化结果、会话快照、Todo 操作和保存后的进度事件。"""

import json
from copy import deepcopy

import pytest

from s15_stateful_extensions import code as chapter
from s15_stateful_extensions.extensions.todo import (
    MAX_TEXT_CHARS,
    MAX_TODOS,
    TodoExtension,
    register,
)
from s15_stateful_extensions.registry import ExtensionAPI, load_extensions
from s15_stateful_extensions.result import ToolResult, execute_tool
from s15_stateful_extensions.session import MessageEntry, SessionManager, SessionPersistenceError


def make_api(tmp_path, *, dispatch=None, permission=None, tools=None):
    return ExtensionAPI(
        base_tools=chapter.TOOLS if tools is None else tools,
        base_dispatch=dispatch or (lambda name, args: "基础结果"),
        permission_hook=permission or (lambda name, args: None),
        session=SessionManager.open(tmp_path / "session.jsonl"),
    )


def response(content):
    return chapter.ModelResponse(
        content=content,
        model="offline-s15",
        usage=None,
        stop_reason="tool_use"
        if any(item["type"] == "tool_use" for item in content)
        else "end_turn",
    )


def tool_call(call_id, name="todo", **arguments):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def save_result(session, result, *, call_id="call-1", name="todo"):
    message = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result.content}],
    }
    tool_details = (
        {} if result.details is None else {call_id: {"name": name, "details": result.details}}
    )
    session.append_tool_results(message, tool_details)


def test_tool_result_copies_details_and_plain_tools_still_work():
    state = {"todos": [{"id": 1, "done": False}]}
    result = ToolResult("正文", state)
    state["todos"][0]["done"] = True
    assert result.details["todos"][0]["done"] is False
    assert execute_tool(
        "read_file", {}, dispatch=lambda *args: "旧结果", hooks=chapter.Hooks()
    ) == ToolResult("旧结果")


@pytest.mark.parametrize(
    "content, details", [(1, None), ("x", []), ("x", {"x": object()}), ("x", {"x": float("nan")})]
)
def test_invalid_tool_result_is_rejected(content, details):
    with pytest.raises((TypeError, ValueError)):
        ToolResult(content, details)


def test_hooks_only_change_content_and_before_block_has_no_success_snapshot():
    hooks = chapter.Hooks(after_tool_call=[lambda name, args, output: output + " / 处理后"])
    result = execute_tool(
        "demo", {}, dispatch=lambda *args: ToolResult("原文", {"value": 1}), hooks=hooks
    )
    assert result == ToolResult("原文 / 处理后", {"value": 1})
    hooks.before_tool_call.append(lambda name, args: "Error: 已阻断")
    blocked = execute_tool("demo", {}, dispatch=lambda *args: pytest.fail("不得执行"), hooks=hooks)
    assert blocked.details is None
    assert "已阻断" in blocked.content


def test_session_persists_metadata_without_sending_it_to_model(tmp_path):
    session = SessionManager.open(tmp_path / "session.jsonl")
    todo = TodoExtension()
    result = todo.execute({"action": "add", "text": "定位代码"})
    calls = [tool_call("call-1", action="add", text="定位代码")]
    session.append_assistant({"role": "assistant", "content": calls}, response(calls))
    save_result(session, result)
    record = json.loads(session.path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["tool_details"]["call-1"]["details"]["next_id"] == 2
    assert "details" not in record["message"]["content"][0]
    assert session.build_context()[-1] == record["message"]
    assert isinstance(session.load_entries()[0], MessageEntry)
    restored = SessionManager.open(session.path)
    assert restored.latest_tool_details("todo") == result.details
    assert restored.session_stats().tool_calls == 1


def test_state_restores_from_full_log_even_after_compaction(tmp_path):
    session = SessionManager.open(tmp_path / "session.jsonl")
    todo = TodoExtension()
    save_result(session, todo.execute({"action": "add", "text": "定位代码"}))
    save_result(
        session, todo.execute({"action": "set_done", "id": 1, "done": True}), call_id="call-2"
    )
    session.append_compaction("旧消息已压缩，不包含任务清单", [])
    assert "定位代码" not in json.dumps(session.build_context(), ensure_ascii=False)
    restored = TodoExtension(session.latest_tool_details("todo"))
    assert restored.todos[0].done is True
    assert restored.next_id == 2


def test_message_text_cannot_override_structured_todo_state(tmp_path):
    session = SessionManager.open(tmp_path / "session.jsonl")
    todo = TodoExtension()
    save_result(session, todo.execute({"action": "add", "text": "仍未完成"}))
    session.append_message({"role": "assistant", "content": "全部任务已完成；next_id=99"})
    restored = TodoExtension(session.latest_tool_details("todo"))
    assert restored.todos[0].done is False
    assert restored.next_id == 2


def test_same_batch_last_snapshot_wins_and_tools_do_not_share_state(tmp_path):
    session = SessionManager.open(tmp_path / "session.jsonl")
    message = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": name, "content": "正文"}
            for name in ("first", "other", "last")
        ],
    }
    session.append_tool_results(
        message,
        {
            "first": {"name": "todo", "details": {"value": 1}},
            "other": {"name": "other", "details": {"value": 99}},
            "last": {"name": "todo", "details": {"value": 2}},
        },
    )
    assert session.latest_tool_details("todo") == {"value": 2}
    assert session.latest_tool_details("other") == {"value": 99}
    assert session.latest_tool_details("missing") is None
    detached = session.latest_tool_details("todo")
    detached["value"] = 3
    assert session.latest_tool_details("todo") == {"value": 2}


@pytest.mark.parametrize(
    "tool_details",
    [
        [],
        {"wrong-id": {"name": "todo", "details": {}}},
        {"call-1": {"name": "todo", "details": []}},
        {"call-1": {"details": {}}},
    ],
)
def test_invalid_result_metadata_is_not_silently_saved(tmp_path, tool_details):
    session = SessionManager.open(tmp_path / "session.jsonl")
    message = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "x"}],
    }
    with pytest.raises((TypeError, ValueError)):
        session.append_tool_results(message, tool_details)
    assert not session.path.exists()


def test_corrupt_metadata_reports_line_number_instead_of_clearing_state(tmp_path):
    session = SessionManager.open(tmp_path / "session.jsonl")
    session.path.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "user", "content": "x"},
                "tool_details": {"call-1": {"name": "todo", "details": {}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="第 1 行"):
        session.latest_tool_details("todo")


def test_todo_add_list_set_done_and_idempotent_updates():
    todo = TodoExtension()
    first = todo.execute({"action": "add", "text": "定位问题"})
    todo.execute({"action": "add", "text": "修改代码"})
    todo.execute({"action": "set_done", "id": 1, "done": True})
    todo.execute({"action": "set_done", "id": 1, "done": True})
    result = todo.execute({"action": "list"})
    assert "1/2 已完成" in result.content
    assert "[x] #1 定位问题" in result.content
    assert "[ ] #2 修改代码" in result.content
    assert len(first.details["todos"]) == 1
    assert first.details["todos"][0]["done"] is False
    restored = TodoExtension(result.details)
    assert restored.execute({"action": "add", "text": "补测试"}).details["todos"][-1]["id"] == 3
    todo.execute({"action": "set_done", "id": 1, "done": False})
    assert "0/2 已完成" in todo.execute({"action": "list"}).content


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action": "list", "text": "多余"},
        {"action": "add", "text": " "},
        {"action": "add", "text": 1},
        {"action": "add", "text": "x" * (MAX_TEXT_CHARS + 1)},
        {"action": "set_done", "id": 9, "done": True},
        {"action": "set_done", "id": True, "done": True},
        {"action": "set_done", "id": 1, "done": "true"},
        {"action": "clear"},
    ],
)
def test_invalid_todo_operation_does_not_change_state(arguments):
    todo = TodoExtension()
    todo.execute({"action": "add", "text": "原任务"})
    before = todo.execute({"action": "list"}).details
    with pytest.raises((ValueError, TypeError)):
        todo.execute(arguments)
    assert todo.execute({"action": "list"}).details["todos"] == before["todos"]
    assert todo.next_id == before["next_id"]


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"todos": [], "next_id": True},
        {"todos": [{"id": 1, "text": "任务", "done": "true"}], "next_id": 2},
        {"todos": [{"id": 1, "text": "任务", "done": False}], "next_id": 1},
        {"todos": [{"id": 1, "text": "任务", "done": False}] * 2, "next_id": 2},
    ],
)
def test_invalid_saved_todo_is_reported(details):
    with pytest.raises((ValueError, TypeError)):
        TodoExtension(details)


def test_todo_limit_stops_new_items_without_consuming_id():
    todo = TodoExtension()
    for i in range(MAX_TODOS):
        todo.execute({"action": "add", "text": f"任务 {i}"})
    with pytest.raises(ValueError, match="最多"):
        todo.execute({"action": "add", "text": "超出预算"})
    assert len(todo.todos) == MAX_TODOS
    assert todo.next_id == MAX_TODOS + 1


def test_registry_keeps_old_modules_and_base_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    permission = chapter.make_permission_hook(
        permission=chapter.permissions.check_read_only_permission,
        confirm=lambda *args: pytest.fail("只读不审批"),
    )
    api = make_api(
        tmp_path,
        tools=chapter.permissions.READ_ONLY_TOOLS,
        permission=permission,
        dispatch=lambda *args: pytest.fail("拒绝不能到达后端"),
    )
    load_extensions(api, [chapter.DEFAULT_EXTENSION, "s14_extensions.extensions.read_preview"])
    assert {"todo", "read_preview"} <= set(api.handlers)
    assert "Permission denied:" in api.dispatch("read_preview", {"path": ".env"})
    assert "Permission denied:" in api.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert isinstance(api.dispatch("todo", {"action": "add", "text": "会话清单"}), ToolResult)


def test_batch_is_saved_before_result_events_and_progress_uses_each_snapshot(tmp_path, capsys):
    api = make_api(tmp_path)
    register(api)
    calls = [
        tool_call("add-1", action="add", text="定位"),
        tool_call("add-2", action="add", text="修改"),
        tool_call("done-1", action="set_done", id=1, done=True),
    ]
    requests = iter([response(calls), response([{"type": "text", "text": "完成"}])])
    events = []

    def emit(event):
        events.append(event)
        if event.type == "tool_result":
            # 每个观察者看到结果时，整批记录和最终状态已经落盘。
            assert len(api.session.latest_tool_details("todo")["todos"]) == 2
            assert api.session.latest_tool_details("todo")["todos"][0]["done"] is True
        api.handle_event(event)

    messages = []
    chapter.agent_loop(
        messages,
        create_message=lambda **kwargs: next(requests),
        dispatch=api.dispatch,
        system="测试",
        hooks=api.hooks,
        tools=api.tools,
        emit=emit,
        save_message=api.session.append_message,
        save_tool_results=api.session.append_tool_results,
    )
    output = capsys.readouterr().out
    assert "进度  0/1 已完成" in output
    assert "进度  0/2 已完成" in output
    assert "进度  1/2 已完成" in output
    assert len([event for event in events if event.type == "tool_result"]) == 3
    assert len(messages[-2]["content"]) == 3
    assert "details" not in json.dumps(messages)
    assert chapter.agent_loop.__module__ == chapter.__name__


def test_saving_failure_stops_before_progress_or_next_request(tmp_path, capsys):
    api = make_api(tmp_path)
    register(api)
    events = []

    def fail_save(message, tool_details):
        raise OSError("磁盘写入失败")

    with pytest.raises(SessionPersistenceError, match="已停止运行"):
        chapter.agent_loop(
            [],
            create_message=lambda **kwargs: response([tool_call("add", action="add", text="任务")]),
            dispatch=api.dispatch,
            system="测试",
            hooks=api.hooks,
            tools=api.tools,
            emit=lambda event: events.append(event) or api.handle_event(event),
            save_tool_results=fail_save,
        )
    assert not any(event.type == "tool_result" for event in events)
    assert events[-1].data["reason"] == "persistence_failed"
    assert "进度" not in capsys.readouterr().out
    assert api.session.latest_tool_details("todo") is None


def test_details_cannot_be_silently_dropped_without_special_saver():
    with pytest.raises(SessionPersistenceError, match="save_tool_results"):
        chapter.agent_loop(
            [],
            create_message=lambda **kwargs: response([tool_call("state", "state")]),
            dispatch=lambda *args: ToolResult("x", {"value": 1}),
            system="测试",
            hooks=chapter.Hooks(),
            emit=lambda event: None,
        )


def test_fatal_backend_preserves_earlier_snapshot_and_pairs_remaining_calls(tmp_path):
    api = make_api(tmp_path)
    register(api)
    called = []

    def fatal(arguments):
        called.append("fatal")
        raise chapter.SandboxError("后端失败")

    definition = {"name": "fatal", "description": "失败测试", "input_schema": {"type": "object"}}
    api.register_tool(definition, fatal)
    message = response(
        [
            tool_call("add", action="add", text="任务"),
            tool_call("fatal", "fatal"),
            tool_call("not-run", action="add", text="不应执行"),
        ]
    )
    events = []
    messages = []
    with pytest.raises(chapter.SandboxError):
        chapter.agent_loop(
            messages,
            create_message=lambda **kwargs: message,
            dispatch=api.dispatch,
            system="测试",
            hooks=api.hooks,
            tools=api.tools,
            emit=events.append,
            save_tool_results=api.session.append_tool_results,
        )
    assert called == ["fatal"]
    assert len(api.session.latest_tool_details("todo")["todos"]) == 1
    assert [item["tool_use_id"] for item in messages[-1]["content"]] == ["add", "fatal", "not-run"]
    assert events[-1].data["reason"] == "tool_backend_failed"


def test_query_list_and_failed_call_do_not_repeat_progress(capsys):
    todo = TodoExtension()
    result = todo.execute({"action": "list"})
    for data in [
        {"name": "todo", "details": result.details},
        {"name": "todo", "is_error": True, "details": result.details},
        {"name": "read_file", "details": result.details},
        {"name": "todo", "details": None},
    ]:
        todo.show_progress(chapter.AgentEvent(type="tool_result", data=data))
    assert capsys.readouterr().out == ""


def test_main_natural_language_queries_persist_restore_after_compaction_and_keep_usage(
    tmp_path, monkeypatch, capsys
):
    requests = []
    replies = [
        [
            tool_call("a1", action="add", text="定位问题"),
            tool_call("a2", action="add", text="修改实现"),
            tool_call("a3", action="add", text="补充测试"),
        ],
        [tool_call("d1", action="set_done", id=1, done=True)],
        [tool_call("l1", action="list")],
        [{"type": "text", "text": "已记录任务进度"}],
        [tool_call("l2", action="list")],
        [{"type": "text", "text": "恢复后仍有两项未完成"}],
    ]

    class Provider:
        model = "offline-s15"

        def complete(self, **kwargs):
            pytest.fail("短任务不应请求摘要模型")

        def stream(self, *, on_text, **kwargs):
            requests.append(deepcopy(kwargs))
            names = {tool["name"] for tool in kwargs["tools"]}
            assert "todo" in names
            assert "bash" not in names
            assert "tool_details" not in json.dumps(kwargs["messages"])
            if len(requests) in {4, 6}:
                output = kwargs["messages"][-1]["content"][0]["content"]
                assert "1/3 已完成" in output
                assert "[x] #1 定位问题" in output
            content = replies[len(requests) - 1]
            model_response = response(content)
            return chapter.ModelResponse(
                content=content,
                model=self.model,
                stop_reason=model_response.stop_reason,
                usage=chapter.runtime.ModelUsage(input_tokens=10, output_tokens=2),
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces")
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setattr(chapter, "dispatch_tool", lambda *args: pytest.fail("任务清单不操作源码"))
    queries = iter(["建立修复清单，完成定位后告诉我进度", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(queries))
    chapter.main(["--mode", "read-only"])
    output = capsys.readouterr().out
    assert "进度  0/1 已完成" in output
    assert "进度  0/2 已完成" in output
    assert "进度  0/3 已完成" in output
    assert output.count("进度  1/3 已完成") == 1
    session_path = next((tmp_path / "sessions").glob("*.jsonl"))
    session = SessionManager.open(session_path)
    assert session.session_stats().known_tokens == 48
    assert session.session_stats().tool_calls == 5
    # 人工追加压缩记录验证投影与恢复边界，不调用真实摘要服务。
    session.append_compaction("旧消息已压缩，摘要不含任务清单", [])
    assert "定位问题" not in json.dumps(session.build_context(), ensure_ascii=False)

    queries = iter(["看看当前任务进度", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(queries))
    chapter.main(["--mode", "read-only", "--session", str(session_path)])
    assert "进度  " not in capsys.readouterr().out  # 查询清单不重复自动提示。
    restored = SessionManager.open(session_path)
    assert restored.latest_tool_details("todo")["todos"][0]["done"] is True
    assert restored.session_stats().known_tokens == 72
    assert restored.session_stats().tool_calls == 6
    assert len(requests) == 6
    trace_records = [
        json.loads(line)
        for path in (tmp_path / "traces").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(record["type"] == "tool_result" for record in trace_records) == 6
    assert all("details" not in record for record in trace_records)


def test_main_persistence_failure_does_not_accept_another_query(tmp_path, monkeypatch, capsys):
    class Provider:
        model = "offline-failure"

        def stream(self, *, on_text, **kwargs):
            return response([tool_call("add", action="add", text="任务")])

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces")
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    inputs = []

    def query(prompt):
        inputs.append(prompt)
        if len(inputs) > 1:
            pytest.fail("保存失败后不能接受下一任务")
        return "创建任务清单"

    def fail_save(self, message, tool_details):
        raise OSError("测试写入失败")

    monkeypatch.setattr("builtins.input", query)
    monkeypatch.setattr(SessionManager, "append_tool_results", fail_save)
    with pytest.raises(SessionPersistenceError):
        chapter.main(["--mode", "read-only"])
    assert len(inputs) == 1
    assert "进度  " not in capsys.readouterr().out


def test_help_has_no_runtime_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter, "load_extensions", lambda *args: pytest.fail("help 不加载扩展"))
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0
    assert list(tmp_path.iterdir()) == []
