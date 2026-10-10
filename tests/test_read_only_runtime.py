"""离线覆盖 s06–s12 的真实入口，验证共用启动选项确实接入执行门禁。"""

from contextlib import contextmanager
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

from s03_permission import code as permissions
from s10_model_provider.code import ModelResponse, ModelUsage

CHAPTERS = [
    "s06_session_context",
    "s07_session_compaction",
    "s08_runtime_events",
    "s09_runtime_streaming",
    "s10_model_provider",
    "s11_runtime_observability",
    "s12_skill_loading",
]


@pytest.fixture(params=CHAPTERS)
def chapter(request):
    return import_module(f"{request.param}.code")


def _response(content):
    return ModelResponse(
        content=content,
        model="offline-test",
        stop_reason="tool_use" if content[0]["type"] == "tool_use" else "end_turn",
        usage=ModelUsage(input_tokens=100, output_tokens=10),
    )


def _calls(calls, prefix):
    return [
        {"type": "tool_use", "id": f"{prefix}-{index}", "name": name, "input": arguments}
        for index, (name, arguments) in enumerate(calls)
    ]


def _offline_model(chapter, monkeypatch, request):
    """只替换 SDK 或 Provider 出口，保留真实请求器、上下文组件和权限 Hook。"""
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_ID", "offline-test")
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("SESSION_COMPACTION_RESERVE_TOKENS", "16384")
    monkeypatch.setenv("SESSION_COMPACTION_KEEP_RECENT_TOKENS", "20000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")

    if hasattr(chapter, "create_model_provider"):
        provider = SimpleNamespace(
            model="offline-test",
            complete=request,
            stream=lambda *, on_text, **kwargs: request(**kwargs),
        )
        monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: provider)
    else:

        @contextmanager
        def stream(**kwargs):
            response = request(**kwargs)
            yield SimpleNamespace(text_stream=[], get_final_message=lambda: response)

        client = SimpleNamespace(messages=SimpleNamespace(create=request, stream=stream))
        monkeypatch.setattr(chapter, "Anthropic", lambda **kwargs: client)


@pytest.mark.parametrize("resume", [False, True], ids=["new", "resume"])
def test_read_only_main_filters_and_blocks_without_approval(
    chapter, tmp_path, monkeypatch, capsys, resume
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(permissions, "WORKDIR", tmp_path)
    source = tmp_path / "README.md"
    source.write_text("项目证据", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=不应进入上下文", encoding="utf-8")
    old_messages = [
        {"role": "user", "content": "以前允许写入"},
        {
            "role": "assistant",
            "content": _calls([("write_file", {"path": "old.txt", "content": "old"})], "old"),
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "old-0", "content": "Wrote old.txt"}
            ],
        },
    ]
    arguments = ["--mode", "read-only"]
    if resume:
        session_path = tmp_path / "history.jsonl"
        session = chapter.SessionManager.open(session_path)
        for message in old_messages:
            session.append_message(message)
        arguments = ["--session", str(session_path), *arguments]

    forbidden = [
        ("bash", {"command": "dir"}),
        ("write_file", {"path": "new.txt", "content": "禁止写入"}),
        ("edit_file", {"path": "README.md", "old_text": "证据", "new_text": "错误"}),
        ("read_file", {"path": ".env"}),
        ("grep", {"path": "../outside.py", "pattern": "secret"}),
        ("unknown", {}),
    ]
    allowed = [
        ("read_file", {"path": "README.md"}),
        ("grep", {"path": "README.md", "pattern": "证据"}),
        ("glob", {"pattern": "*.md"}),
    ]
    requests = []

    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        assert kwargs["tools"] is permissions.READ_ONLY_TOOLS
        if len(requests) == 1:
            return _response(_calls(forbidden, "blocked"))
        if len(requests) == 2:
            results = kwargs["messages"][-1]["content"]
            assert len(results) == len(forbidden)
            assert all("denied" in result["content"].lower() for result in results)
            return _response(_calls(allowed, "allowed"))
        assert len(requests) == 3
        results = kwargs["messages"][-1]["content"]
        assert "项目证据" in results[0]["content"]
        assert "README.md:1:" in results[1]["content"]
        assert "README.md" in results[2]["content"]
        return _response([{"type": "text", "text": "只读分析完成"}])

    _offline_model(chapter, monkeypatch, request)
    inputs = iter(["解释代码", "q"])

    def read_input(prompt):
        assert "是否允许" not in prompt, "只读模式不应请求批准"
        return next(inputs)

    monkeypatch.setattr("builtins.input", read_input)
    chapter.main(arguments)

    if not resume:
        session_path = next(chapter.SESSION_ROOT.glob("*.jsonl"))
    messages = chapter.SessionManager.open(session_path).build_context()
    new_messages = messages[len(old_messages) if resume else 0 :]
    assert len(requests) == 3
    assert [message["role"] for message in new_messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert new_messages[-1]["content"][0]["text"] == "只读分析完成"
    assert "不应进入上下文" not in session_path.read_text(encoding="utf-8")
    assert not (tmp_path / "old.txt").exists()
    assert not (tmp_path / "new.txt").exists()
    assert source.read_text(encoding="utf-8") == "项目证据"
    assert "工具模式：read-only" in capsys.readouterr().out
    if resume:
        assert messages[: len(old_messages)] == old_messages


def test_default_main_keeps_write_approval(chapter, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(permissions, "WORKDIR", tmp_path)
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        assert kwargs["tools"] is chapter.TOOLS
        if len(requests) == 1:
            return _response(
                _calls([("write_file", {"path": "new.txt", "content": "new"})], "write")
            )
        assert kwargs["messages"][-1]["content"][0]["content"].startswith("Wrote")
        return _response([{"type": "text", "text": "写入完成"}])

    _offline_model(chapter, monkeypatch, request)
    inputs = iter(["创建文件", "y", "q"])
    approvals = []

    def read_input(prompt):
        if "是否允许" in prompt:
            approvals.append(prompt)
        return next(inputs)

    monkeypatch.setattr("builtins.input", read_input)
    chapter.main([])
    assert len(approvals) == 1
    assert len(requests) == 2
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("arguments, exit_code", [(["--mode", "unrestricted"], 2), (["--help"], 0)])
def test_cli_exits_before_initialization(chapter, tmp_path, monkeypatch, arguments, exit_code):
    monkeypatch.chdir(tmp_path)

    def unexpected(**kwargs):
        pytest.fail("参数退出时不应加载配置、创建客户端或写入会话")

    monkeypatch.setattr(chapter, "load_dotenv", unexpected)
    with pytest.raises(SystemExit) as error:
        chapter.main(arguments)
    assert error.value.code == exit_code
    assert list(tmp_path.iterdir()) == []


def test_empty_tools_do_not_restore_defaults(chapter):
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        return _response([{"type": "text", "text": "直接回答"}])

    # s08 起循环要求显式观察者；测试只丢弃展示事件，不替换运行流程。
    event_options = {"emit": lambda event: None} if hasattr(chapter, "AgentEvent") else {}
    chapter.agent_loop(
        [],
        create_message=request,
        dispatch=lambda name, arguments: pytest.fail("不应调用工具"),
        system="test",
        hooks=chapter.Hooks(),
        tools=[],
        **event_options,
    )
    assert requests[0]["tools"] == []
