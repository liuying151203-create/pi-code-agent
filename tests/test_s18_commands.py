"""s18：Pi 风格效果选择、真实持久化和同步模型触发的离线契约。"""

import ast
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

import tutorials.s17_configuration.code as previous
import tutorials.s18_commands.code as chapter
from pi_code_agent import ModelResponse, ModelUsage, cli, create_agent
from pi_code_agent.command_entry import CommandEntry
from pi_code_agent.commands import CommandRegistry
from pi_code_agent.compaction_base import CompactionPolicy, estimate_context_tokens
from pi_code_agent.events import EventDispatchError, TerminalEventSink
from pi_code_agent.extension_entries import CustomEntry, CustomMessageEntry
from pi_code_agent.extensions.todo import register as register_todo
from pi_code_agent.observable_session import SessionStats
from pi_code_agent.requests import estimate_session_context_tokens
from pi_code_agent.session import JsonlSessionStore, SessionManager, SessionPersistenceError


def answer(content=None, reason="end_turn"):
    return ModelResponse(
        content or [{"type": "text", "text": "完成"}], "offline-s18", reason, ModelUsage(100, 10)
    )


class Provider:
    """记录实际请求，额外调用直接失败，不访问真实服务。"""

    model = "offline-s18"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.summaries = []

    def stream(self, *, on_text, **kwargs):
        self.calls.append(deepcopy(kwargs))
        result = self.responses.pop(0)
        for block in result.content:
            if block["type"] == "text":
                on_text(block["text"])
        return result

    def complete(self, **kwargs):
        self.summaries.append(deepcopy(kwargs))
        return answer([{"type": "text", "text": "保留任务目标和扩展明确发送的事实。"}])


@pytest.fixture(params=[create_agent, chapter.create_agent], ids=["package", "tutorial"])
def make_agent(request, tmp_path):
    workspace = tmp_path / "target"
    workspace.mkdir()

    def make(provider=None, **options):
        return request.param(
            provider=provider or Provider(),
            workspace=workspace,
            data_dir=tmp_path / "records",
            **options,
        )

    return make


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_builtin_notifications_and_audit_are_not_model_messages(make_agent, capsys):
    agent = make_agent()
    agent.subscribe(TerminalEventSink())
    assert agent.commands.get("todos") is None
    agent.register_extension(register_todo)
    for command in ["/help", "/stats", "/todos"]:
        assert not agent.execute_command(command).is_error
    output = capsys.readouterr().out
    assert "/todos" in output and "模型请求 0 次" in output and "暂无任务" in output
    assert agent.session.build_context() == []
    assert agent.session.session_stats() == SessionStats()
    assert agent.usage.stats.model_requests == 0
    assert [entry["status"] for entry in records(agent.session.path)] == [
        "started",
        "succeeded",
    ] * 3
    assert any(entry["type"] == "notification" for entry in records(agent.trace_path))
    agent.close()


def test_args_and_persistent_state_are_independent_from_context(make_agent):
    agent = make_agent()
    payload = {"values": [1]}
    agent.extensions.register_command(
        "note",
        lambda args, ctx: agent.extensions.append_entry("note", {"text": args}),
        description="保存笔记",
    )
    agent.execute_command('/note 路径带空格 "a b"')
    agent.extensions.append_entry("data", payload)
    payload["values"].append(2)
    saved = agent.session.load_entries()
    assert [entry.data for entry in saved if isinstance(entry, CustomEntry)] == [
        {"text": '路径带空格 "a b"'},
        {"values": [1]},
    ]
    assert agent.session.build_context() == []
    path = agent.session.path
    agent.close()
    restored = make_agent(session_path=path)
    assert restored.session.load_entries() == saved
    assert restored.session.build_context() == []
    restored.close()


@pytest.mark.parametrize("display", [False, True])
@pytest.mark.parametrize("trigger", [False, True])
def test_display_and_trigger_are_independent_and_message_is_saved_first(
    make_agent, capsys, display, trigger
):
    provider = Provider(answer())
    agent = make_agent(provider)
    agent.subscribe(TerminalEventSink())
    events = []
    agent.subscribe(events.append)
    details = {"private_state": [1]}

    def handler(args, ctx):
        agent.extensions.send_message(
            "明确发送的状态",
            custom_type="status",
            display=display,
            details=details,
            trigger_turn=trigger,
        )
        assert provider.calls == []
        details["private_state"].append(2)

    agent.extensions.register_command("status", handler, description="测试效果组合")
    assert not agent.execute_command("/status").is_error
    output = capsys.readouterr().out
    assert ("明确发送的状态" in output) == display
    context = agent.session.build_context()
    assert context[0] == {"role": "user", "content": "明确发送的状态"}
    assert "private_state" not in str(context)
    entries = agent.session.load_entries()
    message = next(entry for entry in entries if isinstance(entry, CustomMessageEntry))
    assert message.details == {"private_state": [1]}
    assert len(provider.calls) == int(trigger)
    if trigger:
        kinds = [event.type for event in events]
        assert kinds.index("command_end") < kinds.index("model_request")
        assert provider.calls[0]["messages"] == [context[0]]
    else:
        agent.run("继续")
        assert provider.calls[0]["messages"] == [context[0], {"role": "user", "content": "继续"}]
    assert agent.session.session_stats().model_requests == 1
    agent.close()


def test_idle_message_api_and_user_message_trigger_same_loop(make_agent):
    provider = Provider(answer(), answer(), answer())
    agent = make_agent(provider)
    agent.extensions.send_message("稍后使用", display=False)
    assert provider.calls == []
    agent.extensions.send_message("现在继续", display=False, trigger_turn=True)
    assert len(provider.calls) == 1
    agent.extensions.send_user_message("真实用户任务")
    assert len(provider.calls) == 2
    assert provider.calls[1]["messages"][-1] == {"role": "user", "content": "真实用户任务"}

    def ask(args, ctx):
        agent.extensions.send_user_message(args)
        assert len(provider.calls) == 2

    agent.extensions.register_command("ask", ask, description="向模型提问")
    agent.execute_command("/ask 请继续检查")
    assert len(provider.calls) == 3
    assert provider.calls[-1]["messages"][-1]["content"] == "请继续检查"
    assert sum(isinstance(entry, CustomMessageEntry) for entry in agent.session.load_entries()) == 2
    agent.close()


def test_handler_failure_preserves_effects_but_does_not_trigger_turn(make_agent):
    provider = Provider(answer())
    agent = make_agent(provider)

    def fail(args, ctx):
        agent.extensions.append_entry("saved", [1])
        agent.extensions.send_message("已经保存的事实", display=False, trigger_turn=True)
        raise ValueError("处理失败")

    agent.extensions.register_command("fail", fail, description="失败路径")
    result = agent.execute_command("/fail")
    assert result.is_error and "处理失败" in result.error
    assert provider.calls == []
    assert agent.session.load_entries()[-1].status == "failed"
    assert agent.session.build_context() == [{"role": "user", "content": "已经保存的事实"}]
    agent.run("核对已保存状态")
    assert len(provider.calls) == 1
    agent.close()


@pytest.mark.parametrize("text", ["/missing", "/help extra", "/", "/bad-return"])
def test_unknown_arguments_and_invalid_return_are_failed_audit_only(make_agent, text):
    agent = make_agent()
    agent.extensions.register_command(
        "bad-return", lambda args, ctx: "不再返回正文", description="错误返回类型"
    )
    result = agent.execute_command(text)
    assert result.is_error
    assert [entry.status for entry in agent.session.load_entries()] == ["started", "failed"]
    assert agent.session.build_context() == []
    agent.close()


def test_todos_restores_same_structured_state_after_compaction(make_agent):
    call = {
        "type": "tool_use",
        "id": "t1",
        "name": "todo",
        "input": {"action": "add", "text": "学习 Pi 消息机制"},
    }
    agent = make_agent(Provider(answer([call], "tool_use"), answer()))
    agent.register_extension(register_todo)
    agent.run("添加待办")
    saved = agent.session.latest_tool_details("todo")
    stats = agent.session.session_stats()
    agent.session.append_compaction("已有任务。", [])
    path = agent.session.path
    agent.close()
    restored = make_agent(session_path=path)
    restored.register_extension(register_todo)
    events = []
    restored.subscribe(events.append)
    restored.execute_command("/todos")
    assert "[ ] #1 学习 Pi 消息机制" in events[-1].data.get("message", "") or any(
        "学习 Pi 消息机制" in event.data.get("message", "") for event in events
    )
    assert restored.session.latest_tool_details("todo") == saved
    assert restored.session.session_stats() == stats
    assert restored.session.build_context() == [
        {"role": "user", "content": "[会话摘要]\n已有任务。"}
    ]
    restored.close()


def test_only_explicit_messages_participate_in_estimation_and_compaction(make_agent):
    provider = Provider(answer(), answer())
    agent = make_agent(
        provider, compaction_policy=CompactionPolicy(512, reserve_tokens=64, keep_recent_tokens=64)
    )
    agent.run("第一项任务")
    agent.extensions.append_entry("private", "扩展私有数据" * 1000)
    agent.execute_command("/help")
    before = estimate_session_context_tokens(agent.session.load_entries())
    assert before == 110
    agent.extensions.send_message("模型事实" * 1000, display=False)
    assert estimate_session_context_tokens(
        agent.session.load_entries()
    ) == 110 + estimate_context_tokens([{"role": "user", "content": "模型事实" * 1000}])
    agent.run("继续")
    assert len(provider.summaries) == 1
    transcript = provider.summaries[0]["messages"][0]["content"]
    assert (
        "模型事实" in transcript and "扩展私有数据" not in transcript and "/help" not in transcript
    )
    assert "模型事实" * 100 not in str(agent.session.build_context())
    assert "模型事实" * 100 in agent.session.path.read_text(encoding="utf-8")
    agent.close()


def test_old_failed_tail_and_missing_tool_pairs_survive_custom_messages(tmp_path):
    session = SessionManager.open(tmp_path / "history.jsonl")
    custom = CustomMessageEntry("status", "用户要求检查", display=False)
    session.append_extension_entry(custom)
    bad = answer(reason="aborted")
    message = {"role": "assistant", "content": bad.content}
    session.append_assistant(message, bad)
    session.append_compaction("摘要", [custom.to_message(), message])
    assert session.build_context() == [
        {"role": "user", "content": "[会话摘要]\n摘要"},
        custom.to_message(),
    ]
    call = {"type": "tool_use", "id": "missing", "name": "read_file", "input": {"path": "x"}}
    session.append_assistant({"role": "assistant", "content": [call]}, answer([call], "tool_use"))
    session.append_extension_entry(CustomMessageEntry("status", "下一条事实"))
    saved = session.path.read_text(encoding="utf-8")
    context = session.build_context()
    assert context[-2]["content"][0]["tool_use_id"] == "missing"
    assert context[-1]["content"] == "下一条事实"
    assert session.path.read_text(encoding="utf-8") == saved


def test_lifecycle_rejects_reentry_and_actions_during_model_execution(make_agent):
    agent = make_agent(Provider(answer()))

    def observe(event):
        if event.type not in {"command_start", "command_end", "model_request"}:
            return
        for action in [
            lambda: agent.run("重入"),
            lambda: agent.execute_command("/help"),
            lambda: agent.extensions.send_message("不能插入工具批次"),
            lambda: agent.extensions.send_user_message("重入用户任务"),
        ]:
            with pytest.raises(RuntimeError, match="运行"):
                action()
        if event.type == "model_request":
            agent.extensions.append_entry("request-observed", {"private": True})

    agent.subscribe(observe)
    agent.execute_command("/help")
    agent.run("模型任务")
    assert any(isinstance(entry, CustomEntry) for entry in agent.session.load_entries())
    assert "private" not in str(agent.session.build_context())
    agent.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        agent.execute_command("/help")


@pytest.mark.parametrize("status", ["started", "succeeded", "effect"])
def test_persistence_failure_closes_agent_and_does_not_announce_success(
    make_agent, monkeypatch, status
):
    agent = make_agent()
    handler = Mock(side_effect=lambda args, ctx: agent.extensions.append_entry("state", 1))
    agent.extensions.register_command("save", handler, description="保存")
    original = agent.session.append_command

    def fail(entry):
        if entry.status == status:
            raise OSError("磁盘不可写")
        original(entry)

    if status == "effect":
        monkeypatch.setattr(
            agent.session, "append_extension_entry", Mock(side_effect=OSError("磁盘不可写"))
        )
    else:
        monkeypatch.setattr(agent.session, "append_command", fail)
    with pytest.raises(SessionPersistenceError, match="保存"):
        agent.execute_command("/save")
    assert handler.call_count == (0 if status == "started" else 1)
    with pytest.raises(RuntimeError, match="已关闭"):
        agent.run("不能继续")
    agent.close()


@pytest.mark.parametrize("kind", ["command_start", "command_end", "custom_message"])
def test_observer_failure_propagates_with_saved_facts(make_agent, kind):
    agent = make_agent()
    agent.extensions.register_command(
        "send",
        lambda args, ctx: agent.extensions.send_message("事实", trigger_turn=True),
        description="发送",
    )

    def observe(event):
        if event.type == kind:
            raise OSError("观察者故障")

    agent.subscribe(observe)
    with pytest.raises(EventDispatchError, match="观察者故障"):
        agent.execute_command("/send")
    entries = agent.session.load_entries()
    assert entries[0].status == "started"
    assert any(isinstance(entry, CustomMessageEntry) for entry in entries) == (
        kind != "command_start"
    )
    assert agent.session.session_stats().model_requests == 0
    agent.close()


def test_interruption_preserves_unknown_audit_without_model_pollution(make_agent):
    agent = make_agent()
    handler = Mock(side_effect=KeyboardInterrupt)
    agent.extensions.register_command("interrupt", handler, description="中断")
    with pytest.raises(KeyboardInterrupt):
        agent.execute_command("/interrupt")
    path = agent.session.path
    agent.close()
    restored = make_agent(session_path=path)
    assert [entry.status for entry in restored.session.load_entries()] == ["started"]
    assert restored.session.build_context() == []
    handler.assert_called_once()
    restored.close()


@pytest.mark.parametrize("name", ["", "/help", "Help", "1bad", "bad name"])
def test_registry_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        CommandRegistry().register(name, lambda args, ctx: None, description="说明")


def test_duplicate_registration_cannot_override_builtins(make_agent):
    agent = make_agent()
    with pytest.raises(ValueError, match="重复"):
        agent.extensions.register_command("help", lambda args, ctx: None, description="不能覆盖")
    agent.close()


@pytest.mark.parametrize(
    "record",
    [
        {
            "type": "command",
            "command_id": "c",
            "command": "/x",
            "status": "failed",
            "timestamp": "now",
        },
        {"type": "custom", "custom_type": "", "data": {}, "timestamp": "now"},
        {
            "type": "custom_message",
            "custom_type": "x",
            "content": "x",
            "display": 1,
            "timestamp": "now",
        },
    ],
)
def test_corrupt_records_report_line_number(tmp_path, record):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="第 1 行"):
        JsonlSessionStore(path).read_all()


@pytest.mark.parametrize("entry", [cli, chapter])
def test_real_cli_routes_notifications_without_injecting_audit(
    entry, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "MODEL_PROVIDER": "anthropic",
            "MODEL_ID": "offline",
            "ANTHROPIC_API_KEY": "offline-only",
            "MODEL_CONTEXT_WINDOW_TOKENS": "128000",
        },
    )
    provider = Provider(answer())
    monkeypatch.setattr(entry, "create_model_provider", Mock(return_value=provider))
    inputs = iter(["/help", "/stats", "/todos", "/unknown", "继续", "q"])
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
            "pi_code_agent.extensions.todo",
        ]
    )
    assert provider.calls[0]["messages"] == [{"role": "user", "content": "继续"}]
    text = capsys.readouterr().out
    assert "可用命令" in text and "暂无任务" in text and "未知命令" in text


def test_tutorial_keeps_complete_previous_core_loop():
    def loop(module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "agent_loop"
        )

    assert ast.dump(loop(chapter)) == ast.dump(loop(previous))


def test_initial_command_format_remains_readable_as_audit(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "command",
                "command_id": "old",
                "command": "/bad",
                "status": "failed",
                "display_text": "旧展示",
                "model_text": "旧错误",
                "timestamp": "old-time",
            }
        ),
        encoding="utf-8",
    )
    session = SessionManager.open(path)
    assert session.load_entries()[0].error == "旧错误"
    assert session.build_context() == []


def test_model_failure_after_successful_command_is_not_command_failure(make_agent):
    agent = make_agent(Provider(answer(reason="error")))
    agent.extensions.register_command(
        "ask",
        lambda args, ctx: agent.extensions.send_message("需要模型处理", trigger_turn=True),
        description="触发模型失败路径",
    )
    with pytest.raises(RuntimeError, match="模型返回失败"):
        agent.execute_command("/ask")
    audit = [entry for entry in agent.session.load_entries() if isinstance(entry, CommandEntry)]
    assert [entry.status for entry in audit] == ["started", "succeeded"]
    assert agent.session.session_stats().failed_requests == 1
    assert all(message["role"] != "assistant" for message in agent.session.build_context())
    agent.close()
