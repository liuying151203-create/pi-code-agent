"""s06：运行状态、持久化屏障和恢复，不把消息序列化当作会话完成保证。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s06_sessions",
        "tutorials.s07_extensions",
        "tutorials.s08_hooks",
        "tutorials.s09_commands",
        "tutorials.s10_queues",
        "tutorials.s11_resources",
    ]
)
def api(request):
    return load_runtime(request.param)


def test_completed_messages_round_trip_and_tool_side_effect_waits_for_save(api, tmp_path):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    observed = []

    async def execute(arguments):
        saved = api.SessionStore.open(file, tmp_path).messages
        assert isinstance(saved[-1], api.AssistantMessage)
        assert saved[-1].content[0].id == "call"
        observed.append(True)
        return api.ToolResult("真实结果")

    model = ScriptedModel(
        api.AssistantMessage((api.ToolCall("call", "probe", {}),), "tool_use"),
        api.AssistantMessage((api.Text("完成"),)),
    )
    session = api.AgentSession(
        model=model, tools=[api.Tool("probe", "probe", execute)], store=store
    )
    history = asyncio.run(session.prompt("执行一次"))
    assert observed == [True]
    assert api.SessionStore.open(file, tmp_path).messages == history
    lines = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "session"
    assert [entry["parent_id"] for entry in lines[1:]] == [None] + [
        entry["id"] for entry in lines[1:-1]
    ]
    assert "pending" not in file.read_text(encoding="utf-8")
    assert not session.busy


def test_resume_uses_saved_messages_and_returns_defensive_copies(api, tmp_path):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    store.append_message(api.UserMessage("first"))
    store.append_message(
        api.AssistantMessage((api.ToolCall("a", "probe", {"nested": {"n": 1}}),), "tool_use")
    )
    store.append_message(api.ToolResultMessage("a", "probe", "old"))
    model = ScriptedModel(api.AssistantMessage((api.Text("next"),)))
    session = api.AgentSession(model=model, store=api.SessionStore.open(file, tmp_path))
    snapshot = session.messages
    snapshot[1].content[0].arguments["nested"]["n"] = 999
    session.store.header["cwd"] = "elsewhere"
    assert session.messages[1].content[0].arguments["nested"]["n"] == 1
    asyncio.run(session.prompt("second"))
    assert [
        message.content
        for message in model.calls[0]["messages"]
        if isinstance(message, api.UserMessage)
    ] == ["first", "second"]
    assert api.SessionStore.open(file, tmp_path).messages == session.messages
    assert session.store.header["cwd"] == str(tmp_path.resolve())


def test_running_state_includes_awaited_end_listeners_and_rejects_concurrent_prompt(api):
    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()
        session = api.AgentSession(model=ScriptedModel(api.AssistantMessage((api.Text("done"),))))

        async def listener(event):
            if event["type"] == "agent_end":
                waiting.set()
                await release.wait()

        session.subscribe(listener)
        task = asyncio.create_task(session.prompt("first"))
        await asyncio.wait_for(waiting.wait(), 2)
        assert session.busy
        with pytest.raises(RuntimeError, match="正在运行"):
            await session.prompt("overlap")
        release.set()
        await asyncio.wait_for(task, 2)
        assert not session.busy

    asyncio.run(run())


def test_observer_interruption_still_persists_real_and_unfinished_tool_results(api, tmp_path):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    execute = AsyncMock(return_value=api.ToolResult("actual"))
    model = ScriptedModel(
        api.AssistantMessage(
            (api.ToolCall("a", "probe", {}), api.ToolCall("b", "probe", {})), "tool_use"
        )
    )
    session = api.AgentSession(
        model=model, tools=[api.Tool("probe", "probe", execute)], store=store
    )

    def fail(event):
        if event["type"] == "tool_execution_end":
            raise ValueError("display failed")

    session.subscribe(fail)
    with pytest.raises(api.EventListenerError):
        asyncio.run(session.prompt("run"))
    assert execute.await_count == 1
    saved = api.SessionStore.open(file, tmp_path).messages
    assert saved == session.messages
    assert saved[-2] == api.ToolResultMessage("a", "probe", "actual")
    assert saved[-1].is_error
    assert not session.busy


def test_store_write_failure_blocks_tool_and_does_not_retry_uncertain_write(
    api, tmp_path, monkeypatch
):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    model = ScriptedModel(api.AssistantMessage((api.ToolCall("a", "probe", {}),), "tool_use"))
    execute = AsyncMock()
    session = api.AgentSession(
        model=model, tools=[api.Tool("probe", "probe", execute)], store=store
    )
    original = Path.open
    count = 0

    def opened(path, *args, **kwargs):
        nonlocal count
        if path == file.resolve() and args and args[0] == "r+b":
            count += 1
            if count == 2:
                raise OSError("disk unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(api.EventListenerError, match="disk unavailable"):
        asyncio.run(session.prompt("run"))
    execute.assert_not_called()
    assert store.failed and count == 2
    assert not session.busy
    with pytest.raises(RuntimeError, match="存储已失败"):
        asyncio.run(session.prompt("again"))
    assert count == 2


@pytest.mark.parametrize("bad", ["duplicate", "wrong_parent", "version", "garbage", "nan"])
def test_corrupt_session_is_rejected_without_rewriting(api, tmp_path, bad):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    store.append_message(api.UserMessage("hello"))
    rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    if bad == "duplicate":
        rows.append(rows[-1])
    elif bad == "wrong_parent":
        rows[1]["parent_id"] = "missing"
    elif bad == "version":
        rows[0]["version"] = True
    elif bad == "nan":
        rows[1]["message"] = {"role": "user", "content": float("nan")}
    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    if bad == "garbage":
        content += "{bad"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        api.SessionStore.open(file, tmp_path)
    assert file.read_text(encoding="utf-8") == content


def test_workspace_mismatch_no_overwrite_and_pending_never_saved(api, tmp_path):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    before = file.read_bytes()
    with pytest.raises(FileExistsError):
        api.SessionStore.create(file, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="工作区"):
        api.SessionStore.open(file, other)
    with pytest.raises(ValueError, match="pending"):
        store.append_message(api.AssistantMessage((), "pending"))
    assert file.read_bytes() == before


def test_stale_writer_or_missing_file_cannot_create_a_headerless_log(api, tmp_path):
    file = tmp_path / "session.jsonl"
    first = api.SessionStore.create(file, tmp_path)
    stale = api.SessionStore.open(file, tmp_path)
    first.append_message(api.UserMessage("first"))
    with pytest.raises(OSError, match="其他写入者"):
        stale.append_message(api.UserMessage("stale"))
    file.unlink()
    with pytest.raises(OSError):
        first.append_message(api.UserMessage("missing"))
    assert not file.exists()


def test_memory_session_keeps_client_ownership_and_cli_can_resume(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = ScriptedModel(
        api.AssistantMessage((api.Text("first"),)), api.AssistantMessage((api.Text("second"),))
    )
    model.aclose = AsyncMock()
    session = api.AgentSession(model=model)
    asyncio.run(session.prompt("a"))
    asyncio.run(session.prompt("b"))
    assert len(session.messages) == 4
    model.aclose.assert_not_called()
    (tmp_path / "README.md").write_text("project", encoding="utf-8")
    file = tmp_path / "saved.jsonl"
    for _ in range(2):
        assert (
            api.cli.main(
                [
                    "--demo",
                    "--session",
                    str(file),
                    "--workspace",
                    str(tmp_path),
                    "--prompt",
                    "read",
                ],
                loop=api.agent_loop,
            )
            == 0
        )
    assert len(api.SessionStore.open(file, tmp_path).messages) == 8


def test_valid_final_record_without_newline_can_be_appended_without_corruption(api, tmp_path):
    file = tmp_path / "session.jsonl"
    store = api.SessionStore.create(file, tmp_path)
    store.append_message(api.UserMessage("完整但没有终止换行"))
    file.write_bytes(file.read_bytes().rstrip(b"\n"))
    resumed = api.SessionStore.open(file, tmp_path)
    resumed.append_message(api.AssistantMessage((api.Text("继续"),)))
    assert api.SessionStore.open(file, tmp_path).messages == [
        api.UserMessage("完整但没有终止换行"),
        api.AssistantMessage((api.Text("继续"),)),
    ]
