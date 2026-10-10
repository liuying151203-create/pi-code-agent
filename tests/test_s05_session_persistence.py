from pathlib import Path
from types import SimpleNamespace

import pytest

import s05_session_persistence.code as chapter


def test_default_session_root_is_scoped_to_s05() -> None:
    assert chapter.DEFAULT_SESSION_ROOT == Path(".sessions/s05")


def test_session_store_round_trips_unicode_and_tool_blocks(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    store = chapter.JsonlSessionStore(path)
    message = chapter.SessionMessage(
        role="assistant",
        content=[{"type": "text", "text": "你好"}],
    )

    store.append(message)
    loaded = store.read_all()

    assert loaded[0].to_message() == {
        "role": "assistant",
        "content": [{"type": "text", "text": "你好"}],
    }
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_session_manager_converts_sdk_objects_before_persisting(tmp_path) -> None:
    manager = chapter.SessionManager.open(tmp_path / "session.jsonl")
    manager.append_message(
        {
            "role": "assistant",
            "content": [SimpleNamespace(type="text", text="完成")],
        }
    )

    assert manager.load_messages() == [
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]}
    ]


def test_store_reports_corrupted_line_number(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"message","role":"user","content":"ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        chapter.JsonlSessionStore(path).read_all()


def test_select_session_path_creates_distinct_new_sessions(tmp_path) -> None:
    first = chapter.select_session_path(None, session_root=tmp_path)
    second = chapter.select_session_path(None, session_root=tmp_path)

    assert first.is_file()
    assert second.is_file()
    assert first != second
    assert first.parent == tmp_path


def test_select_session_path_loads_explicit_history(tmp_path) -> None:
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        '{"type":"message","role":"user","content":"继续"}\n',
        encoding="utf-8",
    )

    selected = chapter.select_session_path(history_path)

    assert selected == history_path
    assert chapter.SessionManager.open(selected).load_messages() == [
        {"role": "user", "content": "继续"}
    ]


def test_select_session_path_rejects_missing_history(tmp_path) -> None:
    missing_path = tmp_path / "missing.jsonl"

    with pytest.raises(ValueError, match="会话文件不存在"):
        chapter.select_session_path(missing_path)


def test_agent_loop_persists_assistant_and_tool_messages() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="tool-1", name="read_file", input={"path": "a.txt"}
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
    ]
    saved: list[chapter.Message] = []

    def create_message(**kwargs):
        return responses.pop(0)

    messages = [{"role": "user", "content": "read a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "file content",
        system="test system",
        hooks=chapter.Hooks(),
        save_message=saved.append,
    )

    assert [message["role"] for message in saved] == ["assistant", "user", "assistant"]
    assert saved[0]["content"][0]["type"] == "tool_use"
    assert saved[1]["content"][0]["type"] == "tool_result"
    assert result[-1]["content"][0]["text"] == "done"


# s05 新增：只读模式仍保存消息；历史中出现过授权不等于当前工具获得授权。
@pytest.mark.parametrize("resume", [False, True])
def test_read_only_main_persists_results_without_restoring_history_permissions(
    tmp_path, monkeypatch, capsys, resume
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_ID", "offline-test")
    monkeypatch.setattr(chapter, "Anthropic", lambda **kwargs: object())
    source = tmp_path / "README.md"
    source.write_text("可读项目介绍", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=不能读取", encoding="utf-8")
    old_messages = [
        {"role": "user", "content": "以前允许写入文件"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "old-write",
                    "name": "write_file",
                    "input": {"path": "old.txt", "content": "old"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "old-write", "content": "Wrote old.txt"},
            ],
        },
    ]
    arguments = ["--mode", "read-only"]
    if resume:
        session_path = tmp_path / "history.jsonl"
        manager = chapter.SessionManager.open(session_path)
        for message in old_messages:
            manager.append_message(message)
        # s05 新增：参数顺序不同也应独立选择历史和当前模式。
        arguments = ["--session", str(session_path), *arguments]

    forbidden_calls = [
        ("bash", {"command": "dir"}),
        ("write_file", {"path": "new.txt", "content": "禁止写入"}),
        ("edit_file", {"path": "README.md", "old_text": "可读", "new_text": "错误"}),
        ("read_file", {"path": ".env"}),
        ("grep", {"path": "../outside.py", "pattern": "secret"}),
        ("unknown", {}),
    ]
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        assert kwargs["tools"] is chapter.permissions.READ_ONLY_TOOLS
        if len(requests) == 1:
            assert kwargs["messages"][:-1] == (old_messages if resume else [])
            content = [
                SimpleNamespace(type="tool_use", id=f"blocked-{index}", name=name, input=args)
                for index, (name, args) in enumerate(forbidden_calls)
            ]
        elif len(requests) == 2:
            assert all(
                result["content"].startswith("Permission denied:")
                for result in kwargs["messages"][-1]["content"]
            )
            content = [
                SimpleNamespace(
                    type="tool_use", id="read", name="read_file", input={"path": "README.md"}
                )
            ]
        else:
            assert kwargs["messages"][-1]["content"][0]["content"] == "可读项目介绍"
            content = [SimpleNamespace(type="text", text="已完成只读分析")]
        return SimpleNamespace(content=content)

    inputs = iter(["分析代码", "q"])

    def input_without_approval(prompt):
        assert "是否允许" not in prompt
        return next(inputs)

    monkeypatch.setattr("builtins.input", input_without_approval)
    monkeypatch.setattr(chapter, "ModelRequester", lambda *args: request)
    chapter.main(arguments)
    if not resume:
        session_path = next((tmp_path / ".sessions" / "s05").glob("*.jsonl"))
    saved = chapter.SessionManager.open(session_path).load_messages()
    prefix_count = len(old_messages) if resume else 0
    assert saved[:prefix_count] == (old_messages if resume else [])
    new_messages = saved[prefix_count:]
    assert len(requests) == 3
    assert [message["role"] for message in new_messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(
        result["content"].startswith("Permission denied:") for result in new_messages[2]["content"]
    )
    assert new_messages[4]["content"][0]["content"] == "可读项目介绍"
    assert new_messages[-1]["content"][0]["text"] == "已完成只读分析"
    assert "不能读取" not in session_path.read_text(encoding="utf-8")
    assert not (tmp_path / "new.txt").exists()
    assert not (tmp_path / "old.txt").exists()
    assert source.read_text(encoding="utf-8") == "可读项目介绍"
    assert "工具模式：read-only" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [[], ["--mode", "default"]])
def test_default_main_still_confirms_writes_and_saves_messages(tmp_path, monkeypatch, arguments):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter.permissions, "WORKDIR", tmp_path)
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_ID", "offline-test")
    monkeypatch.setattr(chapter, "Anthropic", lambda **kwargs: object())
    requests = []
    approvals = []
    inputs = iter(["创建文件", "q"])

    def terminal_input(prompt):
        if "是否允许" in prompt:
            approvals.append(prompt)
            return "y"
        return next(inputs)

    def request(**kwargs):
        requests.append(kwargs)
        assert kwargs["tools"] is chapter.TOOLS
        if len(requests) == 1:
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="write",
                        name="write_file",
                        input={"path": "new.txt", "content": "new"},
                    )
                ]
            )
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="完成")])

    monkeypatch.setattr("builtins.input", terminal_input)
    monkeypatch.setattr(chapter, "ModelRequester", lambda *args: request)
    chapter.main(arguments)
    assert len(approvals) == 1
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new"
    session_path = next((tmp_path / ".sessions" / "s05").glob("*.jsonl"))
    saved = chapter.SessionManager.open(session_path).load_messages()
    assert [message["role"] for message in saved] == ["user", "assistant", "user", "assistant"]
    assert saved[2]["content"][0]["content"].startswith("Wrote")


def test_empty_tool_list_does_not_restore_defaults():
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="直接回答")])

    chapter.agent_loop(
        [],
        create_message=request,
        dispatch=chapter.dispatch_tool,
        system="test",
        hooks=chapter.Hooks(),
        tools=[],
    )
    assert requests[0]["tools"] == []


def test_invalid_mode_fails_before_model_or_session_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def unexpected(**kwargs):
        pytest.fail("无效模式不应初始化模型或创建会话")

    monkeypatch.setattr(chapter, "load_dotenv", unexpected)
    with pytest.raises(SystemExit) as error:
        chapter.main(["--mode", "unrestricted"])
    assert error.value.code == 2
    assert not (tmp_path / ".sessions").exists()


@pytest.mark.parametrize("arguments", [[], ["--mode", "read-only", "--session", "history.jsonl"]])
def test_shared_parser_only_returns_options_without_creating_files(
    tmp_path, monkeypatch, arguments
):
    monkeypatch.chdir(tmp_path)
    options = chapter.parse_session_args(arguments)
    assert options.mode == ("read-only" if arguments else "default")
    assert options.session == (Path("history.jsonl") if arguments else None)
    assert list(tmp_path.iterdir()) == []
