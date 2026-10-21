"""s21 离线回归：模拟容器协议，真实运行编排/检索/交付检查；不启动 Docker。"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from retrieval_helpers import FakeZoekt, payload

from s11_runtime_observability.code import ModelUsage
from s13_a_sandbox_copy.sandbox import SandboxError
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from s13_a_sandbox_copy.workspace_sync import WorkspaceSyncError
from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.session import SessionManager
from s16_repository_search.index import RepositoryIndex
from s16_repository_search.workspace import EXCLUDED_DIRS
from s19_retrieval_fusion.retrieval import CodeRetriever
from s20_git_task_context.context import TaskContext
from s21_sandbox_repair import code as chapter
from s21_sandbox_repair.delivery import VerifiedDelivery
from s21_sandbox_repair.sandbox import DEFAULT_IMAGE, SandboxExecutor
from s21_sandbox_repair.view import ContainerRepositoryView, snapshot_fingerprint
from s21_sandbox_repair.worker_snapshot import (
    EXTRA_DIRS,
    capture_project,
    protected_path,
    source_files,
)
from s21_sandbox_repair.workflow import AgentRuntime, RepairRunner, TestVerifier
from s21_sandbox_repair.workspace import RepositoryWorkspace


class FakeExecutor:
    """仅模拟隔离协议，不代表 Docker 的 OS 防护已被实测。"""

    def __init__(self, files):
        self.files = dict(files)
        self.ready = True
        self.exits = [0]
        self.calls = []

    @property
    def is_ready(self):
        return self.ready

    def ensure_ready(self):
        if not self.ready:
            raise SandboxError("后端不可用")

    def fail(self, reason):
        self.ready = False
        raise SandboxError(reason)

    def capture_source(self):
        self.ensure_ready()
        return WorkspaceSnapshot(dict(self.files))

    def capture_workspace(self):
        self.ensure_ready()
        return WorkspaceSnapshot(dict(self.files))

    def dispatch(self, name, args):
        self.ensure_ready()
        self.calls.append((name, args))
        if name == "write_file":
            self.files[args["path"]] = args["content"].encode()
        return "ok"

    def verify(self, argv):
        self.calls.append(("verify", list(argv)))
        return {"exit_code": self.exits.pop(0), "output": "test feedback", "truncated": False}


@pytest.fixture
def components(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("HOST ORIGINAL\n", encoding="utf-8")
    workspace = RepositoryWorkspace(root, tmp_path)
    files = {
        "app.py": b"def add(a, b):\n    return a - b\n",
        "tests/test_app.py": b"# public acceptance tests\n",
    }
    executor = FakeExecutor(files)
    view = ContainerRepositoryView(workspace=workspace, executor=executor)
    run_root = tmp_path / "run"
    run_root.mkdir()
    delivery = VerifiedDelivery(
        source_root=root,
        executor=executor,
        baseline=WorkspaceSnapshot(dict(files)),
        report_path=run_root / "changes.diff",
    )
    verifier = TestVerifier(
        executor=executor, argv=["python", "-m", "unittest"], baseline=WorkspaceSnapshot(files)
    )
    return workspace, executor, view, delivery, verifier


def test_view_reads_and_indexes_container_not_host(components):
    workspace, executor, view, _, _ = components
    assert "return a - b" in view.read_file("app.py")
    assert "HOST ORIGINAL" not in view.read_file("/work/app.py")
    index = RepositoryIndex(workspace=view, backend=FakeZoekt())
    retriever = CodeRetriever(workspace=view, index_manager=index)
    first = payload(retriever.search("add", mode="symbol"))
    assert first["status"] == "ok"
    executor.files["app.py"] = b"def add(a,b):\n    return a+b\n"
    with pytest.raises(ValueError, match="版本"):
        view.read_file("app.py")
    second = payload(retriever.search("add", mode="symbol"))
    assert first["index_generation"] != second["index_generation"]
    assert "return a+b" in view.read_file("app.py")
    assert (workspace.root / "app.py").read_text() == "HOST ORIGINAL\n"


@pytest.mark.parametrize(
    "path", ["../secret", "D:/repo/app.py", "/etc/passwd", ".indexes/a", ".env", "tests"]
)
def test_container_read_does_not_escape_or_treat_directory_as_file(components, path):
    _, _, view, _, _ = components
    with pytest.raises((TypeError, ValueError)):
        view.read_file(path)


def test_write_dispatch_only_reaches_same_executor(components):
    workspace, executor, view, _, _ = components
    view.dispatch("write_file", {"path": "new.py", "content": "value=3\n"})
    assert executor.files["new.py"] == b"value=3\n"
    assert not (workspace.root / "new.py").exists()
    assert {tool["name"] for tool in view.tools} == {
        "glob",
        "grep",
        "read_file",
        "write_file",
        "edit_file",
        "bash",
    }


def test_snapshot_protocol_rejects_cache_in_source_and_disables_backend(components):
    _, executor, view, _, _ = components
    executor.capture_source = lambda: WorkspaceSnapshot({".indexes/secret": b"hidden"})
    with pytest.raises(SandboxError):
        view.scan()
    assert not executor.ready


def test_fatal_source_error_cannot_be_swallowed_by_retrieval_wrapper(components, tmp_path):
    _, executor, view, _, _ = components
    retriever = CodeRetriever(
        workspace=view, index_manager=RepositoryIndex(workspace=view, backend=FakeZoekt())
    )
    session = SessionManager.open(tmp_path / "session.jsonl")
    api = ExtensionAPI(
        base_tools=view.tools,
        base_dispatch=view.dispatch,
        permission_hook=view.permission_hook,
        session=session,
    )
    api.register_tool(
        {"name": "search_code", "description": "search", "input_schema": {"type": "object"}},
        lambda args: retriever.search("add"),
    )
    executor.capture_source = lambda: executor.fail("模拟 Docker 故障")
    agent = AgentRuntime(
        loop=chapter.agent_loop,
        requester=Mock(),
        session=session,
        extensions=api,
        emit=Mock(),
        executor=executor,
        system="test",
        max_rounds=3,
    )
    with pytest.raises(SandboxError):
        agent.dispatch("search_code", {})


def test_real_local_loop_saves_tool_results_with_fake_model(components, tmp_path):
    _, executor, view, _, _ = components
    session = SessionManager.open(tmp_path / "session.jsonl")
    api = ExtensionAPI(
        base_tools=view.tools,
        base_dispatch=view.dispatch,
        permission_hook=view.permission_hook,
        session=session,
    )
    responses = iter(
        [
            SimpleNamespace(
                content=[
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "write_file",
                        "input": {"path": "new.py", "content": "value=1\n"},
                    }
                ],
                stop_reason="tool_use",
                model="offline-test",
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            SimpleNamespace(
                content=[{"type": "text", "text": "已修改"}],
                stop_reason="end_turn",
                model="offline-test",
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
        ]
    )
    agent = AgentRuntime(
        loop=chapter.agent_loop,
        requester=lambda **kwargs: next(responses),
        session=session,
        extensions=api,
        emit=Mock(),
        executor=executor,
        system="test",
        max_rounds=3,
    )
    messages = agent.run("新增文件")
    assert len(messages) == 4 and executor.files["new.py"] == b"value=1\n"
    assert session.path.exists()


def test_failed_verification_retries_and_only_real_pass_marks_delivery(components):
    _, executor, _, delivery, verifier = components
    executor.exits = [1, 0]
    prompts = []

    def run(prompt):
        prompts.append(prompt)
        if len(prompts) == 2:
            executor.files["app.py"] = b"def add(a,b):\n    return a+b\n"

    agent = SimpleNamespace(run=run)
    repair = RepairRunner(
        agent=agent,
        verifier=verifier,
        sync=delivery,
        report_path=delivery.report_path.parent / "task.json",
        max_attempts=3,
    )
    report = repair.run(TaskContext({"request": "修复加法"}))
    assert report["status"] == "tests_passed"
    assert [attempt["exit_code"] for attempt in report["attempts"]] == [1, 0]
    assert "verification_feedback" in prompts[1]
    assert delivery.verified_fingerprint == snapshot_fingerprint(executor.capture_workspace())
    assert json.loads(repair.report_path.read_text(encoding="utf-8"))["status"] == "tests_passed"


def test_model_claim_of_success_not_enough_and_attempts_bounded(components):
    _, executor, _, delivery, verifier = components
    executor.exits = [1, 1]
    agent = Mock()
    agent.run.return_value = "所有测试都成功"
    repair = RepairRunner(
        agent=agent,
        verifier=verifier,
        sync=delivery,
        report_path=delivery.report_path.parent / "task.json",
        max_attempts=2,
    )
    assert repair.run(TaskContext({"request": "修复"}))["status"] == "exhausted"
    assert agent.run.call_count == 2 and delivery.verified_fingerprint is None
    with pytest.raises(WorkspaceSyncError, match="没有验证"):
        delivery.apply()


def test_model_error_does_not_retry_as_test_failure(components):
    _, _, _, delivery, verifier = components
    agent = Mock()
    agent.run.side_effect = RuntimeError("模型请求失败")
    repair = RepairRunner(
        agent=agent,
        verifier=verifier,
        sync=delivery,
        report_path=delivery.report_path.parent / "task.json",
        max_attempts=3,
    )
    with pytest.raises(RuntimeError):
        repair.run(TaskContext({"request": "修复"}))
    assert agent.run.call_count == 1
    assert json.loads(repair.report_path.read_text())["status"] == "failed"


@pytest.mark.parametrize("path", ["tests/test_app.py", "pytest.ini", "conftest.py"])
def test_acceptance_tampering_fails_before_verification(components, path):
    _, executor, _, _, verifier = components
    executor.files[path] = b"# weaken checks\n"
    with pytest.raises(SandboxError, match="验收"):
        verifier.run()
    assert not executor.calls


def test_verification_mutating_source_is_not_current_version_pass(components):
    _, executor, _, _, verifier = components

    def verify(argv):
        executor.files["app.py"] = b"changed during tests"
        return {"exit_code": 0, "output": "passed", "truncated": False}

    executor.verify = verify
    with pytest.raises(SandboxError, match="源码发生变化"):
        verifier.run()


def test_zero_unittest_cases_is_not_accepted(components):
    _, executor, _, _, verifier = components
    executor.verify = lambda argv: {
        "exit_code": 0,
        "output": "\nRan 0 tests in 0.0s\nOK",
        "truncated": False,
    }
    with pytest.raises(SandboxError, match="没有执行"):
        verifier.run()


def test_new_task_invalidates_previous_pass(components):
    _, executor, _, delivery, verifier = components
    delivery.preview()
    delivery.mark_verified(delivery.preview_fingerprint)
    repair = RepairRunner(
        agent=SimpleNamespace(run=lambda prompt: None),
        verifier=verifier,
        sync=delivery,
        report_path=delivery.report_path.parent / "task.json",
        max_attempts=1,
    )
    executor.exits = [1]
    repair.run(TaskContext({"request": "下一项"}))
    assert delivery.verified_fingerprint is None


def test_worker_capture_excludes_indexes_before_reading(tmp_path):
    root = tmp_path / "repo"
    (root / ".indexes").mkdir(parents=True)
    (root / ".indexes/huge.bin").write_bytes(b"x" * 100)
    (root / "app.py").write_bytes(b"x=1\n")
    assert capture_project(root).files == {"app.py": b"x=1\n"}
    assert EXCLUDED_DIRS <= EXTRA_DIRS | {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "build",
        "dist",
    }
    assert protected_path(".env")


def test_worker_uses_actual_nested_gitignore_without_affecting_delivery(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("hidden=1\n", encoding="utf-8")
    (root / "visible.py").write_text("shown=2\n", encoding="utf-8")
    project = capture_project(root)
    source = source_files(project, root, git_dir=tmp_path / "ignore-metadata")
    assert "ignored.py" in project.files and "ignored.py" not in source.files
    assert "visible.py" in source.files and not (root / ".git").exists()


def test_executor_defaults_and_unknown_protocol_fail_closed():
    executor = SandboxExecutor(runner=lambda *args, **kwargs: b'{"fatal":"failed"}')
    assert executor.image == DEFAULT_IMAGE
    executor._ready = True
    with pytest.raises(SandboxError):
        executor.verify(["python", "-m", "unittest"])
    with pytest.raises(SandboxError):
        executor.ensure_ready()


def test_help_has_local_loop_and_no_docker_start(monkeypatch):
    monkeypatch.setattr(chapter, "SandboxExecutor", Mock(side_effect=AssertionError("不能启动")))
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0
    tree = ast.parse(Path(chapter.__file__).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "agent_loop" for node in tree.body
    )


def test_structured_model_error_cannot_reach_test_acceptance(components, tmp_path):
    _, executor, view, delivery, verifier = components
    session = SessionManager.open(tmp_path / "failed-model.jsonl")
    api = ExtensionAPI(
        base_tools=view.tools,
        base_dispatch=view.dispatch,
        permission_hook=view.permission_hook,
        session=session,
    )
    response = SimpleNamespace(
        content=[],
        stop_reason="error",
        model="offline-test",
        usage=ModelUsage(input_tokens=2, output_tokens=1),
    )
    agent = AgentRuntime(
        loop=chapter.agent_loop,
        requester=lambda **kwargs: response,
        session=session,
        extensions=api,
        emit=Mock(),
        executor=executor,
        system="test",
        max_rounds=2,
    )
    repair = RepairRunner(
        agent=agent,
        verifier=verifier,
        sync=delivery,
        report_path=delivery.report_path.parent / "failed-task.json",
        max_attempts=3,
    )
    with pytest.raises(RuntimeError, match="模型"):
        repair.run(TaskContext({"request": "修复"}))
    assert not any(name == "verify" for name, _ in executor.calls)


def test_verified_delivery_applies_only_matching_patch_and_advances_baseline(components):
    workspace, executor, _, delivery, verifier = components
    # 仅临时副本调整为与复制基线相同，再真实验证 s13-A 的 git apply/冲突检查。
    (workspace.root / "app.py").write_bytes(executor.files["app.py"])
    executor.files["app.py"] = b"def add(a,b):\n    return a+b\n"
    result = verifier.run()
    delivery.preview()
    delivery.mark_verified(result["source_fingerprint"])
    assert delivery.apply() == 1
    assert (workspace.root / "app.py").read_bytes() == executor.files["app.py"]
    assert delivery.verified_fingerprint is None
    assert delivery.preview() == ""


def test_host_conflict_after_verified_preview_is_not_overwritten(components):
    workspace, executor, _, delivery, verifier = components
    executor.files["app.py"] = b"def add(a,b):\n    return a+b\n"
    result = verifier.run()
    delivery.preview()
    delivery.mark_verified(result["source_fingerprint"])
    before = (workspace.root / "app.py").read_bytes()
    with pytest.raises(WorkspaceSyncError, match="已变化"):
        delivery.apply()
    assert (workspace.root / "app.py").read_bytes() == before


def test_container_change_after_verification_requires_new_validation(components):
    _, executor, _, delivery, verifier = components
    result = verifier.run()
    delivery.preview()
    delivery.mark_verified(result["source_fingerprint"])
    executor.files["app.py"] = b"unverified change\n"
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="没有验证"):
        delivery.apply()


def test_worker_rejects_business_directory_alias_not_silent_deletion(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "business").mkdir(parents=True)
    (root / "business/app.py").write_text("value=1\n", encoding="utf-8")
    original = Path.is_symlink
    # 模拟操作系统将该业务目录报告为链接，不需要 Windows 创建链接权限。
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "business" or original(path))
    with pytest.raises(ValueError, match="目录出现链接"):
        capture_project(root)


def test_grep_output_budget_and_evidence_near_long_line_match(components):
    _, executor, view, _, _ = components
    executor.files["long.py"] = ("前" * 700 + "needle" + "后" * 700 + "\n").encode()
    result = view.grep("long.py", "needle")
    assert "needle" in result
    executor.files["many.py"] = ("前" * 80 + "needle" + "后" * 400 + "\n").encode() * 100
    result = view.grep("many.py", "needle", limit=50)
    assert len(result.encode()) <= 50 * 1024 and "截断" in result


def test_container_close_even_when_verify_only_reports_failure(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_app.py").write_bytes(b"# public test\n")
    (root / "app.py").write_bytes(b"x=1\n")
    executor = FakeExecutor({"tests/test_app.py": b"# public test\n", "app.py": b"x=1\n"})
    executor.name = "offline-run"
    executor.exits = [1]
    executor.start = Mock(side_effect=lambda snapshot, run_root: run_root.mkdir(parents=True))
    executor.close = Mock()
    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path)
    monkeypatch.setattr(chapter, "SandboxExecutor", lambda **kwargs: executor)
    monkeypatch.setattr(chapter, "ZoektBackend", Mock(side_effect=AssertionError("不能启动检索")))
    with pytest.raises(SystemExit) as error:
        chapter.main(["--workspace", str(root), "--verify-only"])
    assert error.value.code == 1
    executor.close.assert_called_once()


@pytest.mark.parametrize("host_conflict", [False, True])
def test_main_assembles_loop_verification_and_confirmed_delivery(
    tmp_path, monkeypatch, host_conflict
):
    root = tmp_path / "project"
    files = {"app.py": b"value=0\n", "tests/test_app.py": b"# public acceptance\n"}
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    executor = FakeExecutor(files)
    executor.name = "offline-run"
    executor.start = Mock(side_effect=lambda snapshot, run_root: run_root.mkdir(parents=True))
    executor.close = Mock()
    responses = iter(
        [
            SimpleNamespace(
                content=[
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "write_file",
                        "input": {"path": "app.py", "content": "value=1\n"},
                    }
                ],
                stop_reason="tool_use",
                model="offline-test",
                usage=ModelUsage(),
            ),
            SimpleNamespace(
                content=[{"type": "text", "text": "已修改，交给 Harness 验证。"}],
                stop_reason="end_turn",
                model="offline-test",
                usage=ModelUsage(),
            ),
        ]
    )
    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path)
    monkeypatch.setattr(chapter, "SandboxExecutor", lambda **kwargs: executor)
    monkeypatch.setattr(chapter, "ZoektBackend", lambda image: FakeZoekt())
    monkeypatch.setattr(chapter, "load_dotenv", Mock())
    monkeypatch.setattr(
        chapter, "create_model_provider", lambda **kwargs: SimpleNamespace(model="offline-test")
    )
    monkeypatch.setattr(chapter, "summary_request_options", lambda provider: {})
    monkeypatch.setattr(
        chapter, "StreamingModelRequester", lambda *args: lambda **kwargs: next(responses)
    )
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "100000")
    monkeypatch.setenv("SESSION_COMPACTION_RESERVE_TOKENS", "16384")
    monkeypatch.setenv("SESSION_COMPACTION_KEEP_RECENT_TOKENS", "20000")
    monkeypatch.setenv("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", "4096")
    inputs = iter(["修改 value", "/apply", "y", "q"])

    def answer(prompt):
        value = next(inputs)
        if value == "/apply" and host_conflict:
            (root / "app.py").write_bytes(b"USER EDIT\n")
        return value

    monkeypatch.setattr("builtins.input", answer)
    chapter.main(["--workspace", str(root)])
    expected = b"USER EDIT\n" if host_conflict else b"value=1\n"
    assert (root / "app.py").read_bytes() == expected
    report = next((tmp_path / ".sandboxes/s21").rglob("task-001.json"))
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "tests_passed"
    assert any(name == "verify" for name, _ in executor.calls)
    executor.close.assert_called_once()
