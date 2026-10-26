from types import SimpleNamespace

import pytest

import tutorials.s01_agent_loop.code as chapter
from pi_code_agent import agent_loop as sdk_loop
from tutorials.s01_agent_loop.code import agent_loop


def test_loop_feeds_tool_result_back_until_model_stops() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    name="bash",
                    input={"command": "echo hello"},
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
    ]
    calls: list[dict] = []

    def create_message(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    messages = [{"role": "user", "content": "say hello"}]
    result = agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, args: f"output for: {args['command']}",
        system="test system",
    )

    assert len(calls) == 2
    assert result[-1]["content"][0].text == "done"
    assert result[-2]["role"] == "user"
    assert result[-2]["content"][0]["type"] == "tool_result"
    assert result[-2]["content"][0]["content"] == "output for: echo hello"


def test_loop_stops_when_model_does_not_call_a_tool() -> None:
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="final")])
    calls = 0

    def create_message(**kwargs):
        nonlocal calls
        calls += 1
        return response

    result = agent_loop(
        [{"role": "user", "content": "answer directly"}],
        create_message=create_message,
        dispatch=lambda name, args: "unreachable",
        system="test system",
    )

    assert calls == 1
    assert result[-1]["content"][0].text == "final"


@pytest.mark.parametrize("loop", [agent_loop, sdk_loop], ids=["tutorial", "sdk"])
@pytest.mark.parametrize(
    ("name", "arguments", "stop_reason"),
    [
        ("missing", {}, "tool_use"),
        ("bash", ["bad"], "tool_use"),
        ("bash", {"command": "must not run"}, "max_tokens"),
    ],
)
def test_invalid_or_incomplete_calls_do_not_execute_and_report_error(
    loop, name, arguments, stop_reason
):
    responses = iter(
        [
            SimpleNamespace(
                content=[{"type": "tool_use", "id": "c", "name": name, "input": arguments}],
                stop_reason=stop_reason,
            ),
            SimpleNamespace(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
        ]
    )
    calls = []
    history = [{"role": "user", "content": "test"}]
    loop(
        history,
        create_message=lambda **kwargs: next(responses),
        dispatch=lambda *args: calls.append(args) or "executed",
        system="test",
        tools=chapter.TOOLS,
    )
    assert calls == []
    result = history[-2]["content"][0]
    assert result["tool_use_id"] == "c"
    assert result["is_error"] is True


@pytest.mark.parametrize("loop", [agent_loop, sdk_loop], ids=["tutorial", "sdk"])
def test_explicit_empty_tools_remain_empty(loop):
    seen = []

    def request(**kwargs):
        seen.append(kwargs["tools"])
        return SimpleNamespace(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    loop([], create_message=request, dispatch=lambda *args: "unreachable", system="test", tools=[])
    assert seen == [[]]


def test_bash_nonzero_exit_is_reported(monkeypatch):
    monkeypatch.setattr(
        chapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="failed", returncode=2),
    )
    assert chapter.run_bash("offline-command").startswith("Error: command exited with code 2")


@pytest.mark.parametrize("loop", [agent_loop, sdk_loop], ids=["tutorial", "sdk"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_tool_exception_is_paired_and_followup_can_recover(loop, error_type):
    responses = iter(
        [
            SimpleNamespace(
                content=[
                    {"type": "tool_use", "id": "c", "name": "bash", "input": {"command": "test"}}
                ],
                stop_reason="tool_use",
            ),
            SimpleNamespace(
                content=[{"type": "text", "text": "recovered"}], stop_reason="end_turn"
            ),
        ]
    )

    def dispatch(name, arguments):
        raise error_type("bad tool input")

    history = []
    loop(
        history,
        create_message=lambda **kwargs: next(responses),
        dispatch=dispatch,
        system="test",
        tools=chapter.TOOLS,
    )
    assert history[-2]["content"][0]["is_error"] is True
    assert "bad tool input" in history[-2]["content"][0]["content"]


@pytest.mark.parametrize("loop", [agent_loop, sdk_loop], ids=["tutorial", "sdk"])
@pytest.mark.parametrize("stop", ["error", "aborted"])
def test_failed_model_response_cannot_execute_tools(loop, stop):
    called = []
    response = SimpleNamespace(
        content=[{"type": "tool_use", "id": "c", "name": "bash", "input": {"command": "never"}}],
        stop_reason=stop,
    )
    with pytest.raises(RuntimeError):
        loop(
            [],
            create_message=lambda **kwargs: response,
            dispatch=lambda *args: called.append(args),
            system="test",
            tools=chapter.TOOLS,
        )
    assert called == []
