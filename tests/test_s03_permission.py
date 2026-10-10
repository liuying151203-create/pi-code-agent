from types import SimpleNamespace

import pytest

import s03_permission.code as chapter


def test_read_only_tools_are_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    assert chapter.check_permission("read_file", {"path": "README.md"}).status is (
        chapter.PermissionStatus.ALLOW
    )
    assert chapter.check_permission("glob", {"pattern": "*.py"}).status is (
        chapter.PermissionStatus.ALLOW
    )


def test_ranged_read_stays_read_only_and_returns_requested_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    arguments = {"path": "code.py", "start_line": 2, "limit": 2}

    decision = chapter.check_permission("read_file", arguments)
    result = chapter.dispatch_tool("read_file", arguments)

    assert decision.status is chapter.PermissionStatus.ALLOW
    assert result.startswith("two\nthree\n\n[")
    assert "start_line=4" in result


def test_writes_need_confirmation_and_path_escape_is_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    decision = chapter.check_permission("write_file", {"path": "note.txt", "content": "one"})
    assert decision.status is chapter.PermissionStatus.ASK

    denied = chapter.check_permission(
        "edit_file", {"path": "../outside.txt", "old_text": "a", "new_text": "b"}
    )
    assert denied.status is chapter.PermissionStatus.DENY


def test_bash_rules_distinguish_allow_ask_and_deny() -> None:
    assert chapter.check_permission("bash", {"command": "dir"}).status is (
        chapter.PermissionStatus.ALLOW
    )
    assert chapter.check_permission("bash", {"command": "del note.txt"}).status is (
        chapter.PermissionStatus.ASK
    )
    assert chapter.check_permission("bash", {"command": "format C:"}).status is (
        chapter.PermissionStatus.DENY
    )


def test_grep_is_read_only_but_outside_paths_are_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("def scan():\n    pass", encoding="utf-8")
    arguments = {"path": "code.py", "pattern": "def scan"}
    assert chapter.check_permission("grep", arguments).status == chapter.PermissionStatus.ALLOW
    assert ":1: def scan" in chapter.dispatch_tool("grep", arguments)
    arguments["path"] = "../outside.py"
    assert chapter.check_permission("grep", arguments).status == chapter.PermissionStatus.DENY
    assert (
        chapter.check_permission(
            "bash", {"command": "cd /d D:\\repo && findstr scan code.py"}
        ).status
        == chapter.PermissionStatus.ASK
    )


def test_execute_tool_returns_denial_without_dispatching() -> None:
    dispatched = False

    def dispatch(name, arguments):
        nonlocal dispatched
        dispatched = True
        return "executed"

    result = chapter.execute_tool(
        "bash",
        {"command": "format C:"},
        dispatch=dispatch,
    )

    assert result.startswith("Permission denied:")
    assert not dispatched


def test_agent_loop_returns_permission_result_to_model() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="tool-1", name="write_file", input={"path": "a.txt"}
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="permission handled")]),
    ]

    def create_message(**kwargs):
        return responses.pop(0)

    messages = [{"role": "user", "content": "write a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "should not run",
        system="test system",
        confirm=lambda name, arguments, reason: False,
    )

    assert result[-2]["content"][0]["content"].startswith("Permission denied:")
    assert result[-1]["content"][0].text == "permission handled"


# s03 新增：验证工具筛选与权限门禁是两层控制，不依赖模型遵守提示词。
def test_read_only_mode_exposes_only_read_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    assert {tool["name"] for tool in chapter.READ_ONLY_TOOLS} == {"read_file", "glob", "grep"}
    for name, arguments in (
        ("read_file", {"path": "README.md"}),
        ("grep", {"path": "code.py", "pattern": "def scan"}),
        ("glob", {"pattern": "*.py"}),
    ):
        assert (
            chapter.check_read_only_permission(name, arguments).status
            is chapter.PermissionStatus.ALLOW
        )


@pytest.mark.parametrize("name", ["bash", "write_file", "edit_file", "unknown"])
def test_read_only_mode_never_confirms_or_dispatches_non_read_tools(name):
    def unexpected(*args):
        pytest.fail("只读拒绝不应请求批准或执行工具")

    result = chapter.execute_tool(
        name,
        {"command": "dir", "path": "new.txt", "content": "禁止写入"},
        dispatch=unexpected,
        confirm=unexpected,
        permission=chapter.check_read_only_permission,
    )
    assert result.startswith("Permission denied:")
    assert "只读模式" in result


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "nested/.env",
        "notes/../.env",
        ".ssh/key",
        ".aws/credentials",
        ".git/config",
        "../outside.py",
    ],
)
@pytest.mark.parametrize("name", ["read_file", "grep"])
def test_read_only_mode_blocks_sensitive_and_outside_paths(tmp_path, monkeypatch, path, name):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    assert (
        chapter.check_read_only_permission(name, {"path": path}).status
        is chapter.PermissionStatus.DENY
    )
    assert (
        chapter.check_read_only_permission(name, {"path": ".env.example"}).status
        is chapter.PermissionStatus.ALLOW
    )


def test_read_only_loop_returns_denials_then_reads_without_confirming(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    source = tmp_path / "code.py"
    source.write_text("def scan():\n    return []\n", encoding="utf-8")
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", id="bash", name="bash", input={"command": "dir"}),
                SimpleNamespace(
                    type="tool_use",
                    id="write",
                    name="write_file",
                    input={"path": "new.txt", "content": "禁止写入"},
                ),
                SimpleNamespace(
                    type="tool_use",
                    id="edit",
                    name="edit_file",
                    input={"path": "code.py", "old_text": "[]", "new_text": "None"},
                ),
                SimpleNamespace(
                    type="tool_use", id="secret", name="read_file", input={"path": ".env"}
                ),
                SimpleNamespace(type="tool_use", id="unknown", name="unknown", input={}),
            ]
        ),
        SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", id="glob", name="glob", input={"pattern": "*.py"}),
                SimpleNamespace(
                    type="tool_use",
                    id="grep",
                    name="grep",
                    input={"path": "code.py", "pattern": "def scan"},
                ),
                SimpleNamespace(
                    type="tool_use", id="read", name="read_file", input={"path": "code.py"}
                ),
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="已完成只读分析")]),
    ]
    requests = []
    dispatched = []

    def request(**kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            assert all(
                result["content"].startswith("Permission denied:")
                for result in kwargs["messages"][-1]["content"]
            )
        return responses.pop(0)

    def dispatch(name, arguments):
        dispatched.append(name)
        return chapter.dispatch_tool(name, arguments)

    def unexpected_confirmation(*args):
        pytest.fail("只读模式不应请求用户批准")

    messages = chapter.agent_loop(
        [{"role": "user", "content": "分析 scan"}],
        create_message=request,
        dispatch=dispatch,
        system="test",
        tools=chapter.READ_ONLY_TOOLS,
        permission=chapter.check_read_only_permission,
        confirm=unexpected_confirmation,
    )
    assert dispatched == ["glob", "grep", "read_file"]
    assert all(request["tools"] == chapter.READ_ONLY_TOOLS for request in requests)
    assert source.read_text(encoding="utf-8") == "def scan():\n    return []\n"
    assert not (tmp_path / "new.txt").exists()
    assert messages[-1]["content"][0].text == "已完成只读分析"


def test_explicit_empty_tool_list_does_not_restore_defaults():
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="直接回答")])

    chapter.agent_loop(
        [], create_message=request, dispatch=chapter.dispatch_tool, system="test", tools=[]
    )
    assert requests[0]["tools"] == []


@pytest.mark.parametrize("arguments", [[], ["--mode", "default"], ["--mode", "read-only"]])
def test_main_selects_matching_tools_and_execution_policy(monkeypatch, tmp_path, capsys, arguments):
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_ID", "offline-test")
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    monkeypatch.setattr(chapter, "Anthropic", lambda **kwargs: object())
    requests = []
    read_only = "read-only" in arguments
    inputs = iter(["创建一个文件", "q"])
    confirmations = []

    def terminal_input(prompt):
        if "是否允许" in prompt:
            assert not read_only
            confirmations.append(prompt)
            return "y"
        return next(inputs)

    def request(**kwargs):
        requests.append(kwargs)
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
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="任务结束")])

    monkeypatch.setattr("builtins.input", terminal_input)
    monkeypatch.setattr(chapter, "ModelRequester", lambda *args: request)
    chapter.main(arguments)
    expected_tools = chapter.READ_ONLY_TOOLS if read_only else chapter.TOOLS
    assert all(request["tools"] == expected_tools for request in requests)
    assert len(confirmations) == (0 if read_only else 1)
    assert (tmp_path / "new.txt").exists() is (not read_only)
    assert f"工具模式：{'read-only' if read_only else 'default'}" in capsys.readouterr().out


def test_invalid_mode_fails_before_model_initialization(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("无效模式不应初始化模型")

    monkeypatch.setattr(chapter, "load_dotenv", unexpected)
    with pytest.raises(SystemExit) as error:
        chapter.main(["--mode", "unrestricted"])
    assert error.value.code == 2
