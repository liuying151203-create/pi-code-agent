"""s13-A 工作区选择和会话归属；只读写临时目录，不启动 Docker。"""

import json
import os
import shutil
from pathlib import Path

import pytest

import tutorials.s13_a_sandbox_copy.code as chapter
from tutorials.s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from tutorials.s13_a_sandbox_copy.workspace import WorkspaceLayout


def test_workspace_paths_are_stable_and_same_names_do_not_share_records(tmp_path):
    harness = tmp_path / "harness"
    first = tmp_path / "one/my-app"
    second = tmp_path / "two/my-app"
    for path in (harness, first, second):
        path.mkdir(parents=True)
    a = WorkspaceLayout.for_path(first, harness)
    same = WorkspaceLayout.for_path(first / ".." / first.name, harness)
    b = WorkspaceLayout.for_path(second, harness)
    assert a == same
    assert a.workspace_id != b.workspace_id
    assert a.source_root == first
    assert a.session_root == harness / ".sessions/s13-a" / a.workspace_id
    assert a.trace_root == harness / ".traces/s13-a" / a.workspace_id
    assert a.sandbox_root == harness / ".sandboxes/s13-a" / a.workspace_id
    assert not a.session_root.exists()
    assert list(first.iterdir()) == []


def test_windows_path_case_uses_same_workspace_id(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows 路径大小写规则")
    project = tmp_path / "CaseProject"
    project.mkdir()
    assert WorkspaceLayout.for_path(project, tmp_path) == WorkspaceLayout.for_path(
        Path(str(project).upper()), tmp_path
    )


def test_invalid_workspace_creates_no_records(tmp_path):
    file = tmp_path / "not-a-directory"
    file.write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="已有项目目录"):
        WorkspaceLayout.for_path(file, tmp_path)
    with pytest.raises(FileNotFoundError):
        WorkspaceLayout.for_path(tmp_path / "missing", tmp_path)
    with pytest.raises(ValueError, match="根目录"):
        WorkspaceLayout.for_path(Path(tmp_path.anchor), tmp_path)
    assert not (tmp_path / ".sessions").exists()


def test_new_sessions_keep_jsonl_format_and_validate_workspace_binding(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    layout = WorkspaceLayout.for_path(project, tmp_path)
    first = layout.select_session_path(None)
    second = layout.select_session_path(None)
    assert first != second
    assert first.read_bytes() == b""
    binding = json.loads(first.with_suffix(".workspace.json").read_text(encoding="utf-8"))
    assert binding == {"workspace": os.path.normcase(str(project)), "session": first.name}
    assert layout.select_session_path(first) == first
    session = chapter.SessionManager.open(first)
    session.append_message({"role": "user", "content": "hello"})
    assert session.build_context() == [{"role": "user", "content": "hello"}]


def test_other_workspace_and_copied_history_are_rejected_without_writing(tmp_path):
    projects = [tmp_path / "first", tmp_path / "second"]
    for project in projects:
        project.mkdir()
    a, b = [WorkspaceLayout.for_path(project, tmp_path) for project in projects]
    history = a.select_session_path(None)
    before = history.read_bytes()
    with pytest.raises(ValueError, match="跨项目"):
        b.select_session_path(history)
    b.session_root.mkdir(parents=True)
    copied = b.session_root / history.name
    shutil.copyfile(history, copied)
    with pytest.raises(ValueError, match="缺少有效工作区绑定"):
        b.select_session_path(copied)
    shutil.copyfile(history.with_suffix(".workspace.json"), copied.with_suffix(".workspace.json"))
    with pytest.raises(ValueError, match="绑定不匹配"):
        b.select_session_path(copied)
    assert history.read_bytes() == before


@pytest.mark.parametrize("binding", [b"not json", b"[]", b"{}", b"x" * 16385])
def test_invalid_binding_cannot_be_overwritten_on_resume(tmp_path, binding):
    layout = WorkspaceLayout.for_path(tmp_path, tmp_path)
    history = layout.select_session_path(None)
    metadata = history.with_suffix(".workspace.json")
    metadata.write_bytes(binding)
    with pytest.raises(ValueError):
        layout.select_session_path(history)
    assert metadata.read_bytes() == binding


def test_hardlinked_history_is_rejected(tmp_path):
    layout = WorkspaceLayout.for_path(tmp_path, tmp_path)
    history = layout.select_session_path(None)
    os.link(history, tmp_path / "alias.jsonl")
    with pytest.raises(ValueError, match="跨项目"):
        layout.select_session_path(history)


def test_old_unbound_history_is_preserved_but_not_auto_adopted(tmp_path):
    layout = WorkspaceLayout.for_path(tmp_path, tmp_path)
    old_root = tmp_path / ".sessions/s13-a"
    old_root.mkdir(parents=True)
    history = old_root / "session-old.jsonl"
    original = b'{"type":"message","message":{"role":"user","content":"old"}}\n'
    history.write_bytes(original)
    with pytest.raises(ValueError, match="跨项目"):
        layout.select_session_path(history)
    assert history.read_bytes() == original
    assert not history.with_suffix(".workspace.json").exists()


def test_renamed_session_cannot_reuse_another_file_binding(tmp_path):
    layout = WorkspaceLayout.for_path(tmp_path, tmp_path)
    history = layout.select_session_path(None)
    copy = history.with_name("renamed.jsonl")
    shutil.copyfile(history, copy)
    shutil.copyfile(history.with_suffix(".workspace.json"), copy.with_suffix(".workspace.json"))
    with pytest.raises(ValueError, match="绑定不匹配"):
        layout.select_session_path(copy)


def test_runtime_directory_redirection_is_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / ".sessions"
    try:
        runtime.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 不允许创建符号链接")
    with pytest.raises(ValueError, match="重定向"):
        WorkspaceLayout.for_path(project, tmp_path)
    assert list(project.iterdir()) == []


def test_project_skills_come_from_copied_content_and_use_container_paths():
    files = {
        "tutorials/s12_skill_loading/skills/review/SKILL.md": (
            "\ufeff---\nname: review\ndescription: 审查代码\n---\n只读正文"
        ).encode("utf-8"),
        "unrelated/SKILL.md": b"not a skill",
    }
    catalog = chapter.load_container_skills(WorkspaceSnapshot(files))
    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "review"
    assert skill.description == "审查代码"
    assert skill.path.as_posix() == "/work/tutorials/s12_skill_loading/skills/review/SKILL.md"
    assert chapter.load_container_skills(WorkspaceSnapshot({})).skills == ()


@pytest.mark.parametrize(
    "text",
    [
        "missing header",
        "---\nname: review",
        "---\nname: [broken\n---",
        "---\n- list\n---",
        "---\nname: other\ndescription: review\n---",
        "---\nname: review\ndescription: ''\n---",
    ],
)
def test_invalid_project_skills_fail_before_model_requests(text):
    snapshot = WorkspaceSnapshot(
        {"tutorials/s12_skill_loading/skills/review/SKILL.md": text.encode("utf-8")}
    )
    with pytest.raises((ValueError, TypeError)):
        chapter.load_container_skills(snapshot)
