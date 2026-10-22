from types import SimpleNamespace

import pytest

import tutorials.s04_hooks.code as chapter


def test_before_hook_can_block_without_dispatching() -> None:
    dispatched = False

    def dispatch(name, arguments):
        nonlocal dispatched
        dispatched = True
        return "executed"

    hooks = chapter.Hooks(
        before_tool_call=[lambda name, arguments: "blocked by test"],
    )
    result = chapter.execute_tool(
        "bash",
        {"command": "dir"},
        dispatch=dispatch,
        hooks=hooks,
    )

    assert result == "blocked by test"
    assert not dispatched


def test_after_hooks_run_in_order_and_receive_previous_result() -> None:
    events: list[str] = []

    def first(name, arguments, result):
        events.append(f"first:{result}")
        return result + ":one"

    def second(name, arguments, result):
        events.append(f"second:{result}")
        return result + ":two"

    result = chapter.execute_tool(
        "bash",
        {"command": "dir"},
        dispatch=lambda name, arguments: "base",
        hooks=chapter.Hooks(after_tool_call=[first, second]),
    )

    assert result == "base:one:two"
    assert events == ["first:base", "second:base:one"]


def test_permission_policy_is_registered_as_before_hook(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter.previous, "WORKDIR", tmp_path)
    hooks = chapter.Hooks(
        before_tool_call=[chapter.make_permission_hook(confirm=lambda *args: False)]
    )

    result = chapter.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "one"},
        dispatch=lambda name, arguments: "should not run",
        hooks=hooks,
    )

    assert result.startswith("Permission denied:")


def test_agent_loop_returns_after_hook_result_to_model() -> None:
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

    def create_message(**kwargs):
        return responses.pop(0)

    hooks = chapter.Hooks(after_tool_call=[lambda name, arguments, result: result + " | observed"])
    messages = [{"role": "user", "content": "read a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "file content",
        system="test system",
        hooks=hooks,
    )

    assert result[-2]["content"][0]["content"] == "file content | observed"


# s04 新增：验证 s03 的只读拒绝进入现有 Hook 链，不另外实现权限规则。
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("bash", {"command": "dir"}),
        ("write_file", {"path": "new.txt", "content": "禁止写入"}),
        ("edit_file", {"path": "code.py", "old_text": "a", "new_text": "b"}),
        ("unknown", {}),
        ("read_file", {"path": ".env"}),
        ("grep", {"path": "../outside.py", "pattern": "secret"}),
    ],
)
def test_read_only_permission_hook_blocks_and_after_hook_observes(
    tmp_path, monkeypatch, name, arguments
):
    monkeypatch.setattr(chapter.previous, "WORKDIR", tmp_path)
    observed = []

    def unexpected(*args):
        pytest.fail("只读阻断后不能请求批准、执行后续前置 Hook 或分发工具")

    def after(name, arguments, result):
        observed.append(result)
        return result

    permission_hook = chapter.make_permission_hook(
        permission=chapter.previous.check_read_only_permission,
        confirm=unexpected,
    )
    hooks = chapter.Hooks(
        before_tool_call=[permission_hook, unexpected],
        after_tool_call=[after],
    )
    result = chapter.execute_tool(name, arguments, dispatch=unexpected, hooks=hooks)
    assert result.startswith("Permission denied:")
    assert observed == [result]


@pytest.mark.parametrize(
    "name, arguments, expected",
    [
        ("glob", {"pattern": "*.py"}, "code.py"),
        ("grep", {"path": "code.py", "pattern": "def scan"}, ":1: def scan"),
        ("read_file", {"path": "code.py"}, "def scan():"),
    ],
)
def test_read_only_tools_execute_between_before_and_after_hooks(
    tmp_path, monkeypatch, name, arguments, expected
):
    monkeypatch.setattr(chapter.previous, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("def scan():\n    return []\n", encoding="utf-8")
    events = []

    def before(name, arguments):
        events.append("before")

    def dispatch(name, arguments):
        events.append("dispatch")
        return chapter.dispatch_tool(name, arguments)

    def after(name, arguments, result):
        events.append("after")
        return result + " | observed"

    def unexpected_confirmation(*args):
        pytest.fail("只读工具不需要批准")

    permission_hook = chapter.make_permission_hook(
        permission=chapter.previous.check_read_only_permission,
        confirm=unexpected_confirmation,
    )
    hooks = chapter.Hooks(
        before_tool_call=[permission_hook, before],
        after_tool_call=[after],
    )
    result = chapter.execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
    assert expected in result
    assert result.endswith(" | observed")
    assert events == ["before", "dispatch", "after"]


def test_read_only_loop_passes_filtered_tools_and_returns_hook_denial():
    requests = []
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="blocked", name="bash", input={"command": "dir"}
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="无法运行命令，改用只读工具")]),
    ]

    def request(**kwargs):
        requests.append(kwargs)
        return responses.pop(0)

    def unexpected(*args):
        pytest.fail("拒绝的调用不应执行或请求批准")

    permission_hook = chapter.make_permission_hook(
        permission=chapter.previous.check_read_only_permission, confirm=unexpected
    )
    messages = chapter.agent_loop(
        [{"role": "user", "content": "分析代码"}],
        create_message=request,
        dispatch=unexpected,
        system="test",
        hooks=chapter.Hooks(before_tool_call=[permission_hook]),
        tools=chapter.previous.READ_ONLY_TOOLS,
    )
    assert all(request["tools"] is chapter.previous.READ_ONLY_TOOLS for request in requests)
    assert messages[-2]["content"][0]["content"].startswith("Permission denied:")
    assert messages[-1]["content"][0].text == "无法运行命令，改用只读工具"


def test_explicit_empty_tool_list_does_not_restore_defaults():
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


@pytest.mark.parametrize("arguments", [[], ["--mode", "default"], ["--mode", "read-only"]])
def test_main_assembles_matching_tools_and_permission_hook(
    monkeypatch, tmp_path, capsys, arguments
):
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_ID", "offline-test")
    monkeypatch.setattr(chapter.previous, "WORKDIR", tmp_path)
    monkeypatch.setattr(chapter, "Anthropic", lambda **kwargs: object())
    requests = []
    inputs = iter(["创建一个文件", "q"])
    confirmations = []
    read_only = "read-only" in arguments

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
        result = kwargs["messages"][-1]["content"][0]["content"]
        assert result.startswith("Permission denied:" if read_only else "Wrote")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="任务结束")])

    monkeypatch.setattr("builtins.input", terminal_input)
    monkeypatch.setattr(chapter, "ModelRequester", lambda *args: request)
    chapter.main(arguments)
    expected_tools = chapter.previous.READ_ONLY_TOOLS if read_only else chapter.TOOLS
    assert all(request["tools"] is expected_tools for request in requests)
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
