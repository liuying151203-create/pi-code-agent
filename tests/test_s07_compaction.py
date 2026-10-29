"""验证压缩切点不拆开工具调用及结果。"""

import asyncio
import json
from importlib import import_module

import pytest
from conftest import ScriptedModel


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_preserves_tool_result_with_its_call(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    history = [
        messages.UserMessage("a" * 40),
        messages.AssistantMessage((messages.ToolCall("1", "read", {"path": "x"}),), "tool_use"),
        messages.ToolResultMessage("1", "read", "b" * 40),
        messages.AssistantMessage((messages.Text("c" * 40),)),
    ]
    entries = [{"id": str(i), "message": message} for i, message in enumerate(history)]
    cut = compaction.find_cut_point(entries, 15)
    assert cut.first_kept == 3
    assert cut.split_turn_start == 0
    cut = compaction.find_cut_point(entries, 21)
    assert cut.first_kept == 1
    assert cut.split_turn_start == 0


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_keeps_recent_user_turn_and_reports_no_work(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    entries = [
        {"id": "1", "message": messages.UserMessage("old" * 20)},
        {"id": "2", "message": messages.AssistantMessage((messages.Text("done"),))},
        {"id": "3", "message": messages.UserMessage("new" * 20)},
    ]
    assert compaction.find_cut_point(entries, 10) == compaction.CutPoint(2, None)
    assert compaction.find_cut_point(entries, 1000) is None
    with pytest.raises(ValueError):
        compaction.find_cut_point(entries, 0)


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_does_not_leave_an_orphaned_final_tool_result(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    entries = [
        {"id": "1", "message": messages.UserMessage("request")},
        {
            "id": "2",
            "message": messages.AssistantMessage(
                (messages.ToolCall("call", "read", {}),), "tool_use"
            ),
        },
        {"id": "3", "message": messages.ToolResultMessage("call", "read", "x" * 100)},
    ]
    # 预算落在末尾的工具结果，但它后面没有可用切点，不能把它单独留下。
    assert compaction.find_cut_point(entries, 1) is None


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_compaction_migrates_v1_with_backup_and_restores_complete_history(package, tmp_path):
    messages = import_module(package + ".messages")
    session = import_module(package + ".session")
    path = tmp_path / "session.jsonl"
    store = session.SessionStore.create(path, tmp_path)
    old_id = store.append_message(messages.UserMessage("old"))
    kept_id = store.append_message(messages.UserMessage("recent"))
    before = path.read_bytes()
    store.append_compaction("summary", kept_id, 12, messages.Usage(5, 2))
    assert path.with_name(path.name + ".v1.bak").read_bytes() == before
    restored = session.SessionStore.open(path, tmp_path)
    assert restored.header["version"] == 2
    assert [entry["id"] for entry in restored.entries[:2]] == [old_id, kept_id]
    assert restored.messages == [messages.UserMessage("old"), messages.UserMessage("recent")]
    assert restored.entries[-1]["first_kept_entry_id"] == kept_id


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_migration_rejects_existing_backup_and_invalid_cut_without_changing_v1(package, tmp_path):
    messages = import_module(package + ".messages")
    session = import_module(package + ".session")
    path = tmp_path / "session.jsonl"
    store = session.SessionStore.create(path, tmp_path)
    tool_id = store.append_message(messages.ToolResultMessage("a", "read", "result"))
    with pytest.raises(ValueError, match="工具结果"):
        store.append_compaction("summary", tool_id, None, None)
    kept_id = store.append_message(messages.UserMessage("recent"))
    before = path.read_bytes()
    backup = path.with_name(path.name + ".v1.bak")
    backup.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        store.append_compaction("summary", kept_id, None, None)
    assert backup.read_bytes() == b"do not overwrite"
    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["version"] == 1


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
@pytest.mark.parametrize("persistent", [False, True])
def test_manual_compaction_projects_summary_and_recovers_complete_history(
    package, persistent, tmp_path
):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    model = ScriptedModel(
        messages.AssistantMessage((messages.Text("first answer"),)),
        messages.AssistantMessage((messages.Text("second answer"),)),
        messages.AssistantMessage((messages.Text("保存的摘要"),), usage=messages.Usage(10, 3)),
        messages.AssistantMessage((messages.Text("third answer"),)),
    )
    store = (
        sessions.SessionStore.create(tmp_path / "session.jsonl", tmp_path) if persistent else None
    )
    agent = sessions.AgentSession(model=model, store=store, keep_recent_tokens=10)

    async def run():
        await agent.prompt("old " * 20)
        await agent.prompt("recent " * 20)
        original = agent.messages
        result = await agent.compact("保留目标")
        assert result.summary == "保存的摘要"
        assert result.usage == messages.Usage(10, 3)
        assert agent.messages == original
        assert model.calls[2]["tools"] == ()
        assert "old " in model.calls[2]["messages"][0].content
        assert "保留目标" in model.calls[2]["messages"][0].content
        await agent.prompt("next")
        request = model.calls[3]["messages"]
        assert request[0].content == "以下是之前会话的摘要：\n保存的摘要"
        assert any(
            isinstance(item, messages.UserMessage) and "recent " in item.content for item in request
        )
        assert not any(
            isinstance(item, messages.UserMessage) and "old " in item.content for item in request
        )
        if persistent:
            reopened = sessions.SessionStore.open(store.path, tmp_path)
            resumed_model = ScriptedModel(messages.AssistantMessage((messages.Text("resumed"),)))
            resumed = sessions.AgentSession(
                model=resumed_model, store=reopened, keep_recent_tokens=10
            )
            assert resumed.messages == agent.messages
            await resumed.prompt("after restart")
            assert resumed_model.calls[0]["messages"][0].content.endswith("保存的摘要")

    asyncio.run(run())


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_failed_summary_does_not_publish_compaction(package, tmp_path):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    model = ScriptedModel(
        messages.AssistantMessage((messages.Text("first"),)),
        messages.AssistantMessage((messages.Text("second"),)),
        messages.AssistantMessage((messages.Text("truncated"),), "length"),
    )
    store = sessions.SessionStore.create(tmp_path / "session.jsonl", tmp_path)
    agent = sessions.AgentSession(model=model, store=store, keep_recent_tokens=10)

    async def run():
        await agent.prompt("old " * 20)
        await agent.prompt("recent " * 20)
        before = store.path.read_bytes()
        with pytest.raises(RuntimeError, match="未正常结束"):
            await agent.compact()
        assert store.path.read_bytes() == before
        assert store.header["version"] == 1
        assert not agent.busy

    asyncio.run(run())


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_second_compaction_includes_previous_summary(package):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    model = ScriptedModel(
        messages.AssistantMessage((messages.Text("one"),)),
        messages.AssistantMessage((messages.Text("two"),)),
        messages.AssistantMessage((messages.Text("first summary"),)),
        messages.AssistantMessage((messages.Text("three"),)),
        messages.AssistantMessage((messages.Text("updated summary"),)),
    )
    agent = sessions.AgentSession(model=model, keep_recent_tokens=10)

    async def run():
        await agent.prompt("first " * 20)
        await agent.prompt("second " * 20)
        await agent.compact()
        with pytest.raises(ValueError, match="没有新的"):
            await agent.compact()
        await agent.prompt("third " * 20)
        await agent.compact()
        prompt = model.calls[4]["messages"][0].content
        assert "first summary" in prompt
        assert "second " in prompt
        assert "first first" not in prompt

    asyncio.run(run())


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_manual_compaction_cancels_active_turn_and_waits_for_cleanup(package):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")

    async def run():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class Model:
            async def complete(self, **kwargs):
                if kwargs["system"] == "你负责整理对话摘要，不执行任务。":
                    return messages.AssistantMessage((messages.Text("summary"),))
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        agent = sessions.AgentSession(model=Model(), keep_recent_tokens=1)
        # 预先保存两条用户消息，随后让真实模型回合阻塞。
        agent._save_message(messages.UserMessage("old " * 20))
        agent._history.append(messages.UserMessage("old " * 20))
        task = asyncio.create_task(agent.prompt("new " * 20))
        await asyncio.wait_for(started.wait(), 2)
        result = await asyncio.wait_for(agent.compact(), 2)
        assert cleaned.is_set()
        assert result.summary == "summary"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not agent.busy

    asyncio.run(run())


def test_product_context_hook_observes_summary_and_recent_messages():
    messages = import_module("pi_code_agent.messages")
    sessions = import_module("pi_code_agent.session")
    extensions = import_module("pi_code_agent.extensions")

    async def run():
        seen = []
        runtime = extensions.ExtensionRuntime()
        await runtime.register(
            lambda ext: ext.on(
                "context",
                lambda event, ctx: (
                    seen.append(event["messages"]) or {"messages": event["messages"]}
                ),
            )
        )
        model = ScriptedModel(
            messages.AssistantMessage((messages.Text("one"),)),
            messages.AssistantMessage((messages.Text("two"),)),
            messages.AssistantMessage((messages.Text("checkpoint"),)),
            messages.AssistantMessage((messages.Text("three"),)),
        )
        agent = sessions.AgentSession(model=model, extensions=runtime, keep_recent_tokens=10)
        try:
            await agent.prompt("old " * 20)
            await agent.prompt("recent " * 20)
            await agent.compact()
            await agent.prompt("next")
            assert isinstance(seen[-1][0], messages.CompactionSummary)
            assert not any(
                isinstance(item, messages.UserMessage) and "old " in item.content
                for item in seen[-1]
            )
            assert model.calls[-1]["messages"][0].content.endswith("checkpoint")
            assert runtime.diagnostics == []
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
@pytest.mark.parametrize("stop_reason", ["tool_use", "stop", "length"])
def test_compaction_rejects_incomplete_tool_pairs_on_persistence(package, stop_reason, tmp_path):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    path = tmp_path / "session.jsonl"
    store = sessions.SessionStore.create(path, tmp_path)
    store.append_message(messages.UserMessage("old"))
    keep = store.append_message(messages.UserMessage("new"))
    store.append_message(
        messages.AssistantMessage((messages.ToolCall("id", "read", {}),), stop_reason)
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="缺少工具结果"):
        store.append_compaction("summary", keep, 5, None)
    assert path.read_bytes() == before


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_migration_rejects_same_size_external_modification(package, tmp_path):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    path = tmp_path / "session.jsonl"
    store = sessions.SessionStore.create(path, tmp_path)
    store.append_message(messages.UserMessage("old"))
    keep = store.append_message(messages.UserMessage("new"))
    modified = path.read_bytes().replace(b'"old"', b'"OLD"')
    path.write_bytes(modified)
    with pytest.raises(OSError, match="其他写入者"):
        store.append_compaction("summary", keep, 5, None)
    assert path.read_bytes() == modified
    assert not path.with_name(path.name + ".v1.bak").exists()


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cancelled_summary_does_not_write_or_leave_session_busy(package, tmp_path):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")

    async def run():
        started = asyncio.Event()

        class Model:
            async def complete(self, **kwargs):
                if kwargs["system"] == "你负责整理对话摘要，不执行任务。":
                    started.set()
                    await asyncio.Event().wait()
                return messages.AssistantMessage((messages.Text("done"),))

        store = sessions.SessionStore.create(tmp_path / "session.jsonl", tmp_path)
        agent = sessions.AgentSession(model=Model(), store=store, keep_recent_tokens=10)
        await agent.prompt("old " * 20)
        await agent.prompt("new " * 20)
        before = store.path.read_bytes()
        task = asyncio.create_task(agent.compact())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.path.read_bytes() == before
        assert not agent.busy

    asyncio.run(run())


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_summary_excludes_thinking_signatures_and_limits_tool_output(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    sessions = import_module(package + ".session")
    model = ScriptedModel(messages.AssistantMessage((messages.Text("summary"),)))
    history = [
        messages.AssistantMessage(
            (
                messages.Thinking("readable thinking", "private signature"),
                messages.Thinking("opaque data", "", True),
                messages.Text("answer"),
            )
        ),
        messages.ToolResultMessage("call", "read", "x" * 2500),
    ]
    asyncio.run(compaction.summarize(model, history, None, None, 100, sessions.encode_message))
    prompt = model.calls[0]["messages"][0].content
    assert "readable thinking" in prompt
    assert "private signature" not in prompt
    assert "opaque data" not in prompt
    assert "x" * 2000 in prompt
    assert "x" * 2001 not in prompt


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_split_turn_summarizes_earlier_history_and_turn_prefix_separately(package, tmp_path):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    store = sessions.SessionStore.create(tmp_path / "session.jsonl", tmp_path)
    for message in [
        messages.UserMessage("first goal"),
        messages.AssistantMessage((messages.Text("first result"),)),
        messages.UserMessage("second goal"),
        messages.AssistantMessage((messages.ToolCall("call", "read", {}),), "tool_use"),
        messages.ToolResultMessage("call", "read", "tool output"),
        messages.AssistantMessage((messages.Text("recent answer " * 20),)),
    ]:
        store.append_message(message)
    model = ScriptedModel(
        messages.AssistantMessage((messages.Text("earlier summary"),), usage=messages.Usage(2, 1)),
        messages.AssistantMessage((messages.Text("turn summary"),), usage=messages.Usage(3, 2)),
    )
    agent = sessions.AgentSession(model=model, store=store, keep_recent_tokens=10)
    result = asyncio.run(agent.compact())
    assert result.summary == "earlier summary\n\nturn summary"
    assert result.usage == messages.Usage(5, 3)
    assert "first goal" in model.calls[0]["messages"][0].content
    assert "second goal" not in model.calls[0]["messages"][0].content
    assert "second goal" in model.calls[1]["messages"][0].content
    assert "tool output" in model.calls[1]["messages"][0].content
    assert all(call["tools"] == () for call in model.calls)


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_migration_failure_keeps_original_file_and_does_not_publish_summary(
    package, tmp_path, monkeypatch
):
    messages = import_module(package + ".messages")
    sessions = import_module(package + ".session")
    store = sessions.SessionStore.create(tmp_path / "session.jsonl", tmp_path)
    model = ScriptedModel(
        messages.AssistantMessage((messages.Text("first"),)),
        messages.AssistantMessage((messages.Text("second"),)),
        messages.AssistantMessage((messages.Text("summary"),)),
    )
    agent = sessions.AgentSession(model=model, store=store, keep_recent_tokens=10)

    async def run():
        await agent.prompt("old " * 20)
        await agent.prompt("recent " * 20)
        before = store.path.read_bytes()

        def fail_replace(*args):
            raise OSError("replace failed")

        monkeypatch.setattr(sessions.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failed"):
            await agent.compact()
        assert store.path.read_bytes() == before
        assert store.path.with_name(store.path.name + ".v1.bak").read_bytes() == before
        assert store.header["version"] == 1
        assert all(entry["type"] != "compaction" for entry in store.entries)
        assert not agent.busy

    asyncio.run(run())
