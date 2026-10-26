"""B017–B020 回归：离线验证响应失败、历史投影和观察层停止，不启动真实沙箱。"""

import json
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

import tutorials.s10_model_provider.code as providers
import tutorials.s11_runtime_observability.code as runtime

CHAPTERS = [
    "tutorials.s11_runtime_observability",
    "tutorials.s12_skill_loading",
    "tutorials.s13_a_sandbox_copy",
    "tutorials.s13_b_sandbox_workspace",
    "tutorials.s13_c_sandbox_native",
    "tutorials.s14_extensions",
    "tutorials.s15_stateful_extensions",
]


@pytest.mark.parametrize(
    "reason", [None, "max_rounds", "request_failed", "persistence_failed", "tool_backend_failed"]
)
def test_trace_preserves_end_reason_without_copying_messages(tmp_path, reason):
    data = {"messages": [{"role": "user", "content": "不应重复写入的正文"}]}
    if reason is not None:
        data["reason"] = reason
    path = tmp_path / "trace.jsonl"
    runtime.JsonlTraceRecorder(path)(runtime.AgentEvent(type="agent_end", data=data))
    saved = json.loads(path.read_text(encoding="utf-8"))["data"]
    assert saved["message_count"] == 1
    assert saved.get("reason") == reason
    assert "messages" not in saved
    assert "不应重复写入" not in path.read_text(encoding="utf-8")


def tool_response(stop_reason="tool_use"):
    return providers.ModelResponse(
        [{"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "README.md"}}],
        "offline",
        stop_reason,
        providers.ModelUsage(input_tokens=10, output_tokens=2),
    )


@pytest.mark.parametrize("chapter_name", CHAPTERS)
@pytest.mark.parametrize("stop_reason", ["error", "aborted"])
def test_failed_tool_response_is_not_replayed_but_usage_survives(
    tmp_path, chapter_name, stop_reason
):
    chapter = import_module(f"{chapter_name}.code")
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    response = tool_response(stop_reason)
    chapter.agent_loop(
        [],
        create_message=lambda **kwargs: response,
        dispatch=lambda *args: pytest.fail("失败回答不能执行工具"),
        system="测试",
        hooks=chapter.Hooks(),
        emit=lambda event: None,
        save_assistant=session.append_assistant,
        save_message=session.append_message,
    )
    restored = chapter.SessionManager.open(session.path)
    assert restored.build_context() == []
    assert restored.session_stats().known_tokens == 12
    assert restored.session_stats().failed_requests == 1
    assert len(restored.load_entries()) == 1


def test_old_compaction_tail_filters_failure_by_position_not_equal_text(tmp_path):
    session = runtime.SessionManager.open(tmp_path / "session.jsonl")
    message = {"role": "assistant", "content": [{"type": "text", "text": "相同正文"}]}
    for reason in ["end_turn", "aborted"]:
        session.append_assistant(
            message,
            providers.ModelResponse(
                message["content"],
                "offline",
                reason,
                providers.ModelUsage(input_tokens=10),
            ),
        )
    # 模拟旧版压缩记录：尾部不带 stop_reason，必须从原投影位置识别失败消息。
    session.append_compaction("旧摘要", [message, message])
    session.append_compaction("再次压缩", [message, message])
    context = runtime.SessionManager.open(session.path).build_context()
    assert context == [{"role": "user", "content": "[会话摘要]\n再次压缩"}, message]
    assert session.session_stats().known_tokens == 20
    assert session.session_stats().failed_requests == 1


def test_new_compaction_never_summarizes_or_retains_failed_response(tmp_path):
    session = runtime.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message({"role": "user", "content": "旧任务" * 800})
    failure = tool_response("aborted")
    session.append_assistant({"role": "assistant", "content": failure.content}, failure)
    received = []

    def summarize(messages, previous_summary):
        received.extend(messages)
        return runtime.SummaryResult("旧任务摘要", "offline", providers.ModelUsage(input_tokens=5))

    compactor = runtime.ContextCompactor(
        session,
        runtime.CompactionPolicy(314, 64, 100),
        summarize,
    )
    assert compactor.compact_if_needed() is not None
    assert all(message["content"] != failure.content for message in received)
    assert "call-1" not in str(session.build_context())
    assert session.session_stats().known_tokens == 17


@pytest.mark.parametrize("partial_result", [False, True])
@pytest.mark.parametrize("next_user", [False, True])
def test_missing_results_are_completed_only_in_context(tmp_path, partial_result, next_user):
    session = runtime.SessionManager.open(tmp_path / "session.jsonl")
    response = tool_response()
    response.content.append({"type": "tool_use", "id": "call-2", "name": "glob", "input": {}})
    session.append_assistant({"role": "assistant", "content": response.content}, response)
    if partial_result:
        session.append_message(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "已经保存的结果"},
                ],
            }
        )
    if next_user:
        session.append_message({"role": "user", "content": "继续"})
    before = session.path.read_bytes()
    context = session.build_context()
    results = context[1]["content"]
    assert [result["tool_use_id"] for result in results] == ["call-1", "call-2"]
    if partial_result:
        assert results[0]["content"] == "已经保存的结果"
    assert results[-1]["content"].startswith("Error:")
    assert "do not replay" in results[-1]["content"]
    assert session.path.read_bytes() == before
    assert session.session_stats().known_tokens == 12
    assert session.build_context() == context


@pytest.mark.parametrize("streaming, unfinished", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("known_usage", [False, True])
def test_response_failure_usage_is_persisted_once(tmp_path, streaming, unfinished, known_usage):
    usage = {"prompt_tokens": 100, "completion_tokens": 20} if known_usage else None
    call = {"id": "bad", "function": {"name": "read_file", "arguments": '{"path":'}}
    if unfinished:
        call["function"]["arguments"] = '{"path":"README.md"}'
    response = {
        "usage": usage,
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}],
    }
    chunks = [
        {
            "usage": usage,
            "choices": [
                {"finish_reason": "tool_calls", "delta": {"tool_calls": [dict(call, index=0)]}}
            ],
        }
    ]
    if unfinished:
        chunks[0]["choices"][0]["finish_reason"] = None
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: iter(chunks) if streaming else response,
            )
        )
    )
    provider = providers.OpenAIChatProvider(client, "offline")
    session = runtime.SessionManager.open(tmp_path / "session.jsonl")
    events = runtime.EventDispatcher()
    trace_path = tmp_path / "trace.jsonl"
    tracker = runtime.UsageTracker()
    events.subscribe(runtime.JsonlTraceRecorder(trace_path))
    events.subscribe(tracker)
    events.subscribe(runtime.SessionErrorRecorder(session))
    events.emit(runtime.AgentEvent(type="agent_start", data={"message_count": 0}))
    requester_type = (
        runtime.StreamingModelRequester if streaming else runtime.BlockingModelRequester
    )
    requester = requester_type(provider, 60, events.emit)
    with pytest.raises(providers.ModelResponseError):
        requester(messages=[], tools=[], system="测试")
    stats = runtime.SessionManager.open(session.path).session_stats()
    assert stats.model_requests == stats.failed_requests == 1
    assert stats.known_tokens == tracker.stats.total_tokens == (120 if known_usage else 0)
    assert stats.unknown_usage_requests == (0 if known_usage else 1)
    assert len(session.load_entries()) == 1
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 3


def test_delta_observer_failure_is_not_rebroadcast_as_model_error():
    received = []
    events = runtime.EventDispatcher()
    events.subscribe(lambda event: received.append(event.type))

    def fail(event):
        if event.type == "assistant_delta":
            raise OSError("观察层故障")

    events.subscribe(fail)
    provider = SimpleNamespace(model="offline", stream=lambda *, on_text, **kwargs: on_text("片段"))
    requester = runtime.StreamingModelRequester(provider, 60, events.emit)
    with pytest.raises(runtime.EventDispatchError) as caught:
        requester(messages=[], tools=[])
    assert isinstance(caught.value.__cause__, OSError)
    assert received == ["model_request", "assistant_delta"]


@pytest.mark.parametrize("chapter_name", CHAPTERS)
@pytest.mark.parametrize("failed_event", ["assistant_message", "tool_call", "tool_result"])
def test_observer_failure_stops_main_and_history_can_be_resumed(
    tmp_path,
    monkeypatch,
    chapter_name,
    failed_event,
):
    chapter = import_module(f"{chapter_name}.code")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("SESSION_COMPACTION_RESERVE_TOKENS", "16384")
    monkeypatch.setenv("SESSION_COMPACTION_KEEP_RECENT_TOKENS", "20000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    requests, inputs, executions, executors = [], [], [], []

    class Provider:
        model = "offline"

        def stream(self, *, on_text, **kwargs):
            requests.append(deepcopy(kwargs))
            if len(requests) == 1:
                return tool_response()
            return providers.ModelResponse(
                [{"type": "text", "text": "已恢复"}],
                self.model,
                "stop",
                providers.ModelUsage(input_tokens=1),
            )

    def dispatch(*args):
        executions.append(args)
        return "只读结果"

    def observe(event):
        if event.type == failed_event:
            raise OSError("离线观察者故障")

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "JsonlTraceRecorder", lambda path: observe)

    def query(prompt):
        inputs.append(prompt)
        if len(inputs) > 1:
            pytest.fail("观察者失败后不能继续接受任务")
        return "读取项目"

    monkeypatch.setattr("builtins.input", query)
    if chapter_name.startswith("tutorials.s13_"):
        harness, project = tmp_path / "harness", tmp_path / "project"
        harness.mkdir()
        project.mkdir()
        monkeypatch.setattr(chapter, "HARNESS_ROOT", harness)

        class Executor:
            def __init__(self, **kwargs):
                self.name = f"offline-{len(executors)}"
                self.closed = False
                executors.append(self)

            def start(self, *args):
                args[-1].mkdir(parents=True)

            def dispatch(self, name, arguments):
                return dispatch(name, arguments)

            def capture_workspace(self):
                from tutorials.s13_a_sandbox_copy.snapshot import WorkspaceSnapshot

                return WorkspaceSnapshot({})

            def close(self):
                self.closed = True

        symbol = {
            "tutorials.s13_a_sandbox_copy": "SandboxExecutor",
            "tutorials.s13_b_sandbox_workspace": "SharedWorkspaceExecutor",
            "tutorials.s13_c_sandbox_native": "NativeSandboxExecutor",
        }[chapter_name]
        monkeypatch.setattr(chapter, symbol, Executor)
        arguments = ["--workspace", str(project)]
        if chapter_name != "tutorials.s13_a_sandbox_copy":
            arguments.append("--allow-write")
    else:
        monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions")
        monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces")
        monkeypatch.setattr(chapter, "dispatch_tool", dispatch)
        arguments = ["--mode", "read-only"]

    with pytest.raises(runtime.EventDispatchError):
        chapter.main(arguments)
    assert len(inputs) == len(requests) == 1
    assert len(executions) == (1 if failed_event == "tool_result" else 0)
    assert all(executor.closed for executor in executors)
    history = next(tmp_path.rglob("session-*.jsonl"))
    session = chapter.SessionManager.open(history)
    context = session.build_context()
    assert context[-1]["content"][0]["type"] == "tool_result"
    assert context[-1]["content"][0]["tool_use_id"] == "call-1"
    before_count = len(executions)
    monkeypatch.setattr(chapter, "JsonlTraceRecorder", lambda path: lambda event: None)
    queries = iter(["继续，但不要重放历史工具", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(queries))
    chapter.main([*arguments, "--session", str(history)])
    assert len(executions) == before_count
    assert len(requests) == 2
    assert all(executor.closed for executor in executors)
