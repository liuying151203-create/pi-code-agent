"""s13-A 交付回归：真实 Git 只修改临时测试目录，不启动容器或调用模型。"""

import os
import subprocess

import pytest

from s13_a_sandbox_copy import workspace_sync
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from s13_a_sandbox_copy.workspace_sync import WorkspaceSync, WorkspaceSyncError


class CopyExecutor:
    """只替换容器出口，宿主检查和 Git 补丁仍按真实流程执行。"""

    def __init__(self, files):
        self.files = dict(files)

    def capture_workspace(self):
        return WorkspaceSnapshot(dict(self.files))


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "existing.py").write_bytes(b"original\n")
    (root / "deleted.py").write_bytes(b"delete me\n")
    baseline = WorkspaceSnapshot.capture(root)
    executor = CopyExecutor(baseline.files)
    reports = tmp_path / "reports"
    reports.mkdir()
    return WorkspaceSync(root, executor, baseline, reports / "changes.diff")


def test_apply_add_modify_delete_and_second_round_without_repeated_changes(delivery):
    delivery.executor.files = {"existing.py": b"first\n", "new dir/中文.py": b"added\n"}
    preview = delivery.preview()
    assert "+first" in preview and "deleted.py" in preview
    assert (delivery.source_root / "existing.py").read_bytes() == b"original\n"
    assert delivery.apply() == 3
    assert (delivery.source_root / "existing.py").read_bytes() == b"first\n"
    assert (delivery.source_root / "new dir/中文.py").read_bytes() == b"added\n"
    assert not (delivery.source_root / "deleted.py").exists()
    assert delivery.report_path.read_bytes() == b""
    assert delivery.report_path.with_name("apply-001.diff").read_text(encoding="utf-8") == preview
    assert delivery.report_path.with_name("before-001.tar").is_file()
    delivery.executor.files["existing.py"] = b"second\n"
    assert "-first" in delivery.preview()
    assert delivery.changed_files == ("existing.py",)
    assert delivery.apply() == 1
    assert delivery.report_path.with_name("apply-002.diff").is_file()
    assert delivery.preview() == "" and delivery.apply() == 0


@pytest.mark.parametrize("content", [b"line\n", b"line", b"line\r\n", "中文\r\n".encode()])
def test_preserves_line_endings_and_missing_final_newline(delivery, content):
    delivery.executor.files["existing.py"] = content
    delivery.preview()
    assert delivery.apply() == 1
    assert (delivery.source_root / "existing.py").read_bytes() == content


@pytest.mark.parametrize("content", [b"changed\r\n", b"changed\n"])
def test_crlf_original_is_compared_and_patched_as_bytes(delivery, content):
    (delivery.source_root / "existing.py").write_bytes(b"original\r\n")
    delivery.baseline = WorkspaceSnapshot.capture(delivery.source_root)
    delivery.executor.files = dict(delivery.baseline.files)
    delivery.executor.files["existing.py"] = content
    delivery.preview()
    assert delivery.apply() == 1
    assert (delivery.source_root / "existing.py").read_bytes() == content


@pytest.mark.parametrize("change", ["modify", "delete", "create"])
def test_host_conflict_rejects_entire_batch(delivery, change):
    delivery.executor.files["another.py"] = b"must not be applied\n"
    if change == "create":
        delivery.executor.files["created.py"] = b"agent\n"
        (delivery.source_root / "created.py").write_bytes(b"user\n")
    else:
        delivery.executor.files["existing.py"] = b"agent\n"
        target = delivery.source_root / "existing.py"
        if change == "delete":
            target.unlink()
        else:
            target.write_bytes(b"user\n")
    baseline = delivery.baseline
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="原仓库目标已变化"):
        delivery.apply()
    assert delivery.baseline is baseline
    assert not (delivery.source_root / "another.py").exists()
    assert not delivery.report_path.with_name("apply-001.diff").exists()


def test_unrelated_host_edits_are_preserved_and_not_synced_back(delivery):
    delivery.executor.files["new.py"] = b"agent\n"
    (delivery.source_root / "existing.py").write_bytes(b"user's unrelated edit\n")
    delivery.preview()
    assert delivery.apply() == 1
    assert (delivery.source_root / "existing.py").read_bytes() == b"user's unrelated edit\n"
    assert delivery.executor.files["existing.py"] == b"original\n"
    assert delivery.preview() == ""
    delivery.executor.files["existing.py"] = b"agent's later edit\n"
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="原仓库目标已变化"):
        delivery.apply()


def test_changed_container_version_requires_new_preview(delivery):
    delivery.executor.files["existing.py"] = b"previewed\n"
    delivery.preview()
    delivery.executor.files["existing.py"] = b"not approved\n"
    with pytest.raises(WorkspaceSyncError, match="重新 /diff"):
        delivery.apply()
    assert (delivery.source_root / "existing.py").read_bytes() == b"original\n"
    delivery.preview()
    assert delivery.apply() == 1


def test_external_diff_edit_is_not_used_as_an_apply_instruction(delivery):
    delivery.executor.files["existing.py"] = b"approved\n"
    delivery.preview()
    delivery.report_path.write_text("not the approved patch", encoding="utf-8")
    assert delivery.apply() == 1
    assert (delivery.source_root / "existing.py").read_bytes() == b"approved\n"


@pytest.mark.parametrize("content", [b"", b"\xff", b"a\0b"])
def test_unsupported_content_does_not_partially_apply(delivery, content):
    delivery.executor.files["new.py"] = b"supported\n"
    delivery.executor.files["unsupported.bin"] = content
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="整批未应用"):
        delivery.apply()
    assert not (delivery.source_root / "new.py").exists()


@pytest.mark.parametrize(
    "name",
    ["CON.txt", "name.", "name ", "stream:secret", "a?.py", "../escape", ".git/config", ".env"],
)
def test_untrusted_and_unportable_paths_are_not_applied(delivery, name):
    delivery.executor.files[name] = b"not allowed\n"
    # 真实执行器先检查相对路径和排除目录；这个替身也需走同样的校验。
    payload = WorkspaceSnapshot(delivery.executor.files).to_payload()
    try:
        current = WorkspaceSnapshot.from_payload(payload)
    except ValueError:
        return
    delivery.executor.files = current.files
    delivery.preview()
    with pytest.raises(WorkspaceSyncError):
        delivery.apply()


def test_case_collisions_in_container_or_host_are_rejected(delivery):
    delivery.executor.files.update({"A.py": b"a\n", "a.py": b"b\n"})
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="大小写冲突"):
        delivery.apply()
    del delivery.executor.files["A.py"]
    (delivery.source_root / "A.py").write_bytes(b"host\n")
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="大小写别名"):
        delivery.apply()


def test_parent_file_cannot_be_replaced_by_directory(delivery):
    delivery.executor.files["existing.py/child"] = b"not allowed\n"
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="文件与目录互换"):
        delivery.apply()


def test_host_hardlink_and_directory_targets_are_rejected(delivery):
    target = delivery.source_root / "existing.py"
    outside = delivery.source_root.parent / "outside.py"
    outside.write_bytes(b"original\n")
    target.unlink()
    os.link(outside, target)
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="独立普通文件"):
        delivery.apply()
    assert outside.read_bytes() == b"original\n"
    target.unlink()
    target.mkdir()
    with pytest.raises(WorkspaceSyncError, match="独立普通文件"):
        delivery.apply()


def test_host_symlink_parent_is_never_followed(delivery):
    outside = delivery.source_root.parent / "outside"
    outside.mkdir()
    try:
        (delivery.source_root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 不允许创建符号链接")
    delivery.executor.files["linked/new.py"] = b"agent\n"
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="是链接"):
        delivery.apply()
    assert not (outside / "new.py").exists()


def test_redirected_root_cannot_change_apply_destination(delivery, monkeypatch):
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()
    original_resolve = type(delivery.source_root).resolve
    unexpected_root = delivery.source_root.parent / "outside"

    def redirected(path, *args, **kwargs):
        if path == delivery.source_root:
            return unexpected_root
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(delivery.source_root), "resolve", redirected)
    with pytest.raises(WorkspaceSyncError, match="根目录已被重定向"):
        delivery.apply()


def test_report_clear_failure_does_not_claim_files_were_not_applied(delivery, monkeypatch):
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()
    original_write = type(delivery.report_path).write_bytes

    def failed_clear(path, data):
        if path == delivery.report_path and data == b"":
            raise OSError("report unavailable")
        return original_write(path, data)

    monkeypatch.setattr(type(delivery.report_path), "write_bytes", failed_clear)
    with pytest.raises(WorkspaceSyncError, match="文件已应用且基线已更新"):
        delivery.apply()
    assert (delivery.source_root / "existing.py").read_bytes() == b"agent\n"
    assert delivery.baseline.files["existing.py"] == b"agent\n"


def test_git_check_failure_does_not_advance_baseline_or_apply(delivery, monkeypatch):
    baseline = delivery.baseline
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()

    def failed(*args, **kwargs):
        raise WorkspaceSyncError("check failed")

    monkeypatch.setattr(workspace_sync, "_git_apply", failed)
    with pytest.raises(WorkspaceSyncError, match="check failed"):
        delivery.apply()
    assert delivery.baseline is baseline
    assert (delivery.source_root / "existing.py").read_bytes() == b"original\n"


def test_host_edit_during_preflight_is_checked_again_before_write(delivery, monkeypatch):
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()

    def changed_during_check(*args, check, **kwargs):
        assert check
        (delivery.source_root / "existing.py").write_bytes(b"concurrent user edit\n")

    monkeypatch.setattr(workspace_sync, "_git_apply", changed_during_check)
    with pytest.raises(WorkspaceSyncError, match="确认期间原仓库目标已变化"):
        delivery.apply()
    assert (delivery.source_root / "existing.py").read_bytes() == b"concurrent user edit\n"


def test_unexpected_written_content_is_reported_and_blocks_further_apply(delivery, monkeypatch):
    baseline = delivery.baseline
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()

    def wrong_result(*args, check, **kwargs):
        if not check:
            (delivery.source_root / "existing.py").write_bytes(b"unexpected\n")

    monkeypatch.setattr(workspace_sync, "_git_apply", wrong_result)
    with pytest.raises(WorkspaceSyncError, match="回写结果与预览不一致"):
        delivery.apply()
    assert delivery.baseline is baseline
    with pytest.raises(WorkspaceSyncError, match="上次实际回写失败"):
        delivery.apply()


def test_write_stage_failure_blocks_further_apply_and_preserves_backups(delivery, monkeypatch):
    baseline = delivery.baseline
    delivery.executor.files["existing.py"] = b"agent\n"
    delivery.preview()

    def failed(*args, check, **kwargs):
        if not check:
            raise WorkspaceSyncError("write failed")

    monkeypatch.setattr(workspace_sync, "_git_apply", failed)
    with pytest.raises(WorkspaceSyncError, match="write failed"):
        delivery.apply()
    assert delivery.baseline is baseline
    assert delivery.report_path.with_name("before-001.tar").is_file()
    delivery.preview()
    with pytest.raises(WorkspaceSyncError, match="上次实际回写失败"):
        delivery.apply()


def test_git_apply_uses_fixed_stdin_and_does_not_inherit_git_target_variables(
    delivery, monkeypatch
):
    calls = []
    monkeypatch.setenv("GIT_WORK_TREE", "outside")
    monkeypatch.setenv("GIT_DIR", "outside/.git")

    def completed(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(workspace_sync.subprocess, "run", completed)
    workspace_sync._git_apply(delivery.source_root, b"fixed patch", check=True)
    arguments, options = calls[0]
    assert arguments == ["git", "apply", "--whitespace=nowarn", "--check", "-"]
    assert options["shell"] is False and options["input"] == b"fixed patch"
    assert "GIT_WORK_TREE" not in options["env"] and "GIT_DIR" not in options["env"]
