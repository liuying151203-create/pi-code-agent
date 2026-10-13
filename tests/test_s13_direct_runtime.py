"""B/C 完整入口和循环回归；后端替身仅用于临时目录，不证明 OS 隔离。"""

from types import SimpleNamespace

import pytest

from s10_model_provider.code import ModelResponse, ModelUsage
from s13_a_sandbox_copy.sandbox import SandboxError
from s13_a_sandbox_copy.workspace import WorkspaceLayout
from s13_b_sandbox_workspace import code as b
from s13_c_sandbox_native import code as c


@pytest.fixture(params=[b, c], ids=["s13-b", "s13-c"])
def chapter(request):
    return request.param


def test_direct_main_writes_before_exit_and_restores_only_matching_version(
    tmp_path, monkeypatch, chapter
):
    harness, project = tmp_path / "harness", tmp_path / "project"
    harness.mkdir()
    project.mkdir()
    monkeypatch.setattr(chapter, "HARNESS_ROOT", harness)
    config_paths = []
    monkeypatch.setattr(
        chapter, "load_dotenv", lambda **kwargs: config_paths.append(kwargs["dotenv_path"])
    )
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    executors = []

    class Executor:
        def __init__(self, *, workspace, **kwargs):
            self.workspace = workspace
            self.name = f"run-{len(executors)}"
            self.closed = False
            executors.append(self)

        def start(self, run_root):
            run_root.mkdir(parents=True)

        def dispatch(self, name, arguments):
            assert not self.closed
            assert name == "write_file"
            (self.workspace / arguments["path"]).write_text(arguments["content"], encoding="utf-8")
            return "written"

        def close(self):
            self.closed = True

    symbol = "SharedWorkspaceExecutor" if chapter is b else "NativeSandboxExecutor"
    monkeypatch.setattr(chapter, symbol, Executor)
    requests = []

    class Provider:
        model = "offline-direct"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return ModelResponse(
                    [
                        {
                            "type": "tool_use",
                            "id": "write",
                            "name": "write_file",
                            "input": {"path": "demo.py", "content": "x = 1\n"},
                        }
                    ],
                    self.model,
                    "tool_use",
                    ModelUsage(10, 10),
                )
            return ModelResponse(
                [{"type": "text", "text": "done"}], self.model, "end_turn", ModelUsage(10, 10)
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    inputs = iter(["write", "/diff", "/apply", "q"])

    def user_input(prompt):
        assert "是否允许" not in prompt and "是否应用" not in prompt
        answer = next(inputs)
        if answer in {"/diff", "/apply", "q"}:
            assert (project / "demo.py").read_text() == "x = 1\n"
            assert not executors[-1].closed
        return answer

    monkeypatch.setattr("builtins.input", user_input)
    arguments = ["--workspace", str(project), "--allow-write"]
    chapter.main(arguments)
    version = "s13-b" if chapter is b else "s13-c"
    layout = WorkspaceLayout.for_path(project, harness, chapter=version)
    history = next(layout.session_root.glob("*.jsonl"))
    session = chapter.SessionManager.open(history)
    assert session.session_stats().model_requests == 2
    assert session.session_stats().tool_calls == 1
    assert next(layout.trace_root.glob("*.jsonl")).exists()
    assert "+x = 1" in next(layout.sandbox_root.glob("*/changes.diff")).read_text()
    assert config_paths == [harness / ".env"]
    assert executors[-1].closed
    monkeypatch.setattr("builtins.input", lambda prompt: "q")
    chapter.main([*arguments, "--session", str(history)])
    assert len(list(layout.session_root.glob("*.jsonl"))) == 1
    # 恢复后立即退出，没有事件，不凭空创建新的 Trace 内容。
    assert len(list(layout.trace_root.glob("*.jsonl"))) == 1
    assert len(executors) == 2
    assert next((layout.sandbox_root / "run-1").glob("changes.diff")).read_bytes() == b""
    assert len(requests) == 2
    # 会话不携带写入授权；恢复时仍必须重新在 CLI 明确选择。
    with pytest.raises(SystemExit):
        chapter.main(["--workspace", str(project), "--session", str(history)])
    assert len(executors) == 2


def test_direct_entry_without_authorization_or_help_does_not_create_runtime(
    tmp_path, monkeypatch, chapter
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path)
    with pytest.raises(SystemExit) as result:
        chapter.main([])
    assert result.value.code == 2
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(SystemExit) as result:
        chapter.main(["--help"])
    assert result.value.code == 0
    assert list(tmp_path.iterdir()) == []


def test_cross_version_or_project_history_is_rejected_before_backend(
    tmp_path, monkeypatch, chapter
):
    project = tmp_path / "project"
    project.mkdir()
    history = WorkspaceLayout.for_path(project, tmp_path).select_session_path(None)
    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path)
    symbol = "SharedWorkspaceExecutor" if chapter is b else "NativeSandboxExecutor"
    monkeypatch.setattr(chapter, symbol, lambda **kwargs: pytest.fail("不能创建后端"))
    with pytest.raises(ValueError, match="跨项目"):
        chapter.main(["--workspace", str(project), "--allow-write", "--session", str(history)])


def test_each_chapter_keeps_complete_loop_and_fills_results_on_fatal_failure(chapter):
    assert chapter.agent_loop.__module__ == chapter.__name__
    response = SimpleNamespace(
        content=[
            {"type": "tool_use", "id": str(index), "name": "bash", "input": {"command": "anything"}}
            for index in range(3)
        ],
        stop_reason="tool_use",
    )
    saved, calls, events = [], [], []

    def failed(name, arguments):
        calls.append(name)
        raise SandboxError("isolation failed")

    with pytest.raises(SandboxError, match="isolation failed"):
        chapter.agent_loop(
            [],
            create_message=lambda **kwargs: response,
            dispatch=failed,
            system=chapter.SYSTEM,
            hooks=chapter.Hooks(),
            emit=events.append,
            save_message=saved.append,
        )
    assert calls == ["bash"]
    assert [block["tool_use_id"] for block in saved[-1]["content"]] == ["0", "1", "2"]
    assert events[-1].data["reason"] == "sandbox_failed"


def test_policy_versions_have_distinct_records_but_same_workspace_identity(tmp_path):
    layouts = [
        WorkspaceLayout.for_path(tmp_path, tmp_path, chapter=version)
        for version in ("s13-a", "s13-b", "s13-c")
    ]
    assert len({layout.workspace_id for layout in layouts}) == 1
    assert len({layout.session_root for layout in layouts}) == 3
    with pytest.raises(ValueError, match="版本"):
        WorkspaceLayout.for_path(tmp_path, tmp_path, chapter="../outside")
