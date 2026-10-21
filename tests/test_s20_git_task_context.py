"""s20：只在临时 Git 仓库验证上下文，不修改真实项目暂存区/配置。"""

import ast
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from s20_git_task_context import code as chapter
from s20_git_task_context.context import MAX_CONTEXT_BYTES, TaskContextBuilder, load_task_material
from s20_git_task_context.git_reader import GitReader
from s20_git_task_context.workspace import RepositoryWorkspace


def git(root, *arguments):
    """测试初始化/提交仅限 tmp_path；禁用用户钩子和签名，不改全局配置。"""
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Offline Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "--quiet")
    (target / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (target / ".env").write_text("SECRET=do-not-expose", encoding="utf-8")
    git(target, "add", "app.py", ".env")
    git(target, "commit", "-qm", "initial")
    workspace = RepositoryWorkspace(target, tmp_path)
    return workspace, TaskContextBuilder(workspace=workspace, git_reader=GitReader(target))


def test_git_changes_and_commit_evidence_without_secret_or_writes(repository):
    workspace, builder = repository
    original_head = git(workspace.root, "rev-parse", "HEAD")
    original_index = (workspace.root / ".git/index").read_bytes()
    (workspace.root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    context = builder.build("分析这次修改")
    assert context.payload["git"]["status"] == "ok"
    assert context.payload["git"]["head"] == original_head
    assert context.payload["git"]["changes"][0]["path"] == "app.py"
    assert "return a - b" in context.message()["content"]
    assert "SECRET" not in context.message()["content"]
    assert git(workspace.root, "rev-parse", "HEAD") == original_head
    assert (workspace.root / ".git/index").read_bytes() == original_index


def test_selected_base_is_explicit_and_invalid_ref_not_replaced_by_head(repository):
    workspace, _ = repository
    git(workspace.root, "tag", "baseline")
    builder = TaskContextBuilder(
        workspace=workspace, git_reader=GitReader(workspace.root), base="baseline"
    )
    assert builder.build("任务").payload["git"]["base"] == git(workspace.root, "rev-parse", "HEAD")
    invalid = TaskContextBuilder(
        workspace=workspace, git_reader=GitReader(workspace.root), base="missing-ref"
    )
    with pytest.raises(RuntimeError):
        invalid.build("任务")


@pytest.mark.parametrize("base", ["--help", "HEAD~1", "HEAD;whoami", "../HEAD", "a//b", ""])
def test_base_does_not_become_options_or_shell(repository, base):
    workspace, _ = repository
    with pytest.raises(ValueError):
        TaskContextBuilder(workspace=workspace, git_reader=GitReader(workspace.root), base=base)


def test_parent_repository_is_not_used_as_target(repository):
    workspace, _ = repository
    child = workspace.root / "nested"
    child.mkdir()
    (child / "demo.py").write_text("value=1\n", encoding="utf-8")
    nested = RepositoryWorkspace(child, workspace.root.parent)
    builder = TaskContextBuilder(workspace=nested, git_reader=GitReader(child))
    context = builder.build("任务").payload
    assert context["git"]["status"] == "unavailable"
    assert "initial" not in json.dumps(context)


def test_unborn_repository_remains_usable(tmp_path):
    target = tmp_path / "new"
    target.mkdir()
    git(target, "init", "--quiet")
    (target / "app.py").write_text("x=1\n", encoding="utf-8")
    workspace = RepositoryWorkspace(target, tmp_path)
    context = TaskContextBuilder(workspace=workspace, git_reader=GitReader(target)).build("任务")
    assert context.payload["git"]["status"] == "unborn"


def test_untrusted_filter_and_fsmonitor_not_executed(repository):
    workspace, builder = repository
    marker = workspace.root / "SHOULD_NOT_EXIST"
    git(workspace.root, "config", "filter.danger.clean", f"echo unsafe > {marker.name}")
    git(workspace.root, "config", "core.fsmonitor", f"echo unsafe > {marker.name}")
    git(workspace.root, "config", "diff.danger.textconv", f"echo unsafe > {marker.name}")
    (workspace.root / ".gitattributes").write_text(
        "*.py filter=danger diff=danger\n", encoding="utf-8"
    )
    (workspace.root / "app.py").write_text("x=2\n", encoding="utf-8")
    assert builder.build("任务").payload["git"]["status"] == "ok"
    assert not marker.exists()


def test_material_is_data_not_configuration_and_is_bounded(repository, tmp_path):
    workspace, _ = repository
    path = tmp_path / "issue.json"
    path.write_text(
        json.dumps(
            {"title": "问题", "body": "资料" * 3000, "comments": ["讨论" * 400] * 12},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    material = load_task_material(path)
    builder = TaskContextBuilder(
        workspace=workspace, git_reader=GitReader(workspace.root), material=material
    )
    context = builder.build("定位问题")
    assert context.payload["truncated"]
    assert context.payload["request"] == "定位问题"
    assert len(context.message()["content"].encode()) <= MAX_CONTEXT_BYTES
    path.write_text('{"command":"rm everything"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_task_material(path)


@pytest.mark.parametrize(
    "material", [[], {"comments": "not-list"}, {"body": 5}, {"comments": [False]}]
)
def test_invalid_material_rejected_before_model(tmp_path, material):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(material), encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        load_task_material(path)


def test_stale_source_does_not_produce_task_context(repository, monkeypatch):
    workspace, builder = repository
    monkeypatch.setattr(workspace, "compute_fingerprint", lambda: "changed")
    with pytest.raises(RuntimeError, match="源码变化"):
        builder.build("任务")


def test_show_context_needs_no_model_or_zoekt(repository, monkeypatch, capsys):
    workspace, _ = repository
    monkeypatch.setattr(chapter, "HARNESS_ROOT", workspace.root.parent)
    monkeypatch.setattr(chapter, "ZoektBackend", Mock(side_effect=AssertionError("不可启动引擎")))
    monkeypatch.setattr(
        chapter, "create_model_provider", Mock(side_effect=AssertionError("不可调用模型"))
    )
    chapter.main(["--workspace", str(workspace.root), "--show-context", "--task", "分析修改"])
    assert json.loads(capsys.readouterr().out)["git"]["status"] == "ok"
    assert not workspace.index_root.exists() and not workspace.session_root.exists()


def test_chapter_has_local_loop_and_help_without_side_effect():
    tree = ast.parse(Path(chapter.__file__).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "agent_loop" for node in tree.body
    )
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0


def test_git_read_disables_implicit_transport_and_signature_program(repository, monkeypatch):
    _, builder = repository
    original = subprocess.Popen
    calls = []

    def capture(command, **options):
        calls.append((command, options.get("env", {})))
        return original(command, **options)

    monkeypatch.setattr("s20_git_task_context.git_reader.subprocess.Popen", capture)
    builder.build("分析")
    reader_calls = [
        (command, env) for command, env in calls if "log.showSignature=false" in command
    ]
    assert reader_calls
    assert all(env["GIT_NO_LAZY_FETCH"] == "1" for _, env in reader_calls)
    assert all(env["GIT_ALLOW_PROTOCOL"] == "" for _, env in reader_calls)
