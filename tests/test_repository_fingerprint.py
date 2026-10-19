"""s16/s17 指纹复核回归：正文只交给首次快照，过滤与容量规则保持相同。"""

import pytest

from s16_repository_search import workspace as source_module
from s17_repository_symbols.workspace import RepositoryWorkspace as SymbolWorkspace


@pytest.fixture(params=[source_module.RepositoryWorkspace, SymbolWorkspace], ids=["s16", "s17"])
def workspace(request, tmp_path):
    target = tmp_path / "target"
    harness = tmp_path / "harness"
    target.mkdir()
    harness.mkdir()
    (target / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return request.param(target, harness)


def test_fingerprint_matches_snapshot_without_constructing_or_retaining_one(workspace, monkeypatch):
    (workspace.root / "other.py").write_text("说明 = '中文'\n", encoding="utf-8")
    (workspace.root / "binary.dat").write_bytes(b"abc\x00def")
    (workspace.root / "invalid.dat").write_bytes(b"\xff\xfe")
    (workspace.root / ".env").write_text("secret", encoding="utf-8")
    (workspace.root / ".indexes").mkdir()
    (workspace.root / ".indexes" / "old.py").write_text("cached", encoding="utf-8")
    snapshot = workspace.scan()
    assert set(snapshot.file_contents) == {"app.py", "other.py"}
    reads = []
    original_read = workspace._read_bytes

    def read(relative):
        reads.append(relative)
        return original_read(relative)

    def forbid_snapshot(*args, **kwargs):
        pytest.fail("指纹复核不应调用 scan 或构造 SourceSnapshot")

    monkeypatch.setattr(workspace, "_read_bytes", read)
    monkeypatch.setattr(workspace, "scan", forbid_snapshot)
    monkeypatch.setattr(source_module, "SourceSnapshot", forbid_snapshot)
    assert workspace.compute_fingerprint() == snapshot.fingerprint
    assert sorted(reads) == ["app.py", "binary.dat", "invalid.dat", "other.py"]


@pytest.mark.parametrize("change", ["modify", "add", "delete", "rename"])
def test_fingerprint_detects_current_paths_and_contents(workspace, change):
    before = workspace.scan().fingerprint
    source = workspace.root / "app.py"
    if change == "modify":
        source.write_text("changed\n", encoding="utf-8")
    elif change == "add":
        (workspace.root / "new.py").write_text("new\n", encoding="utf-8")
    elif change == "delete":
        source.unlink()
    else:
        source.rename(workspace.root / "renamed.py")
    assert workspace.compute_fingerprint() != before
    assert workspace.compute_fingerprint() == workspace.scan().fingerprint


def test_fingerprint_checks_total_budget_and_propagates_read_errors(workspace, monkeypatch):
    monkeypatch.setattr(source_module, "MAX_SOURCE_BYTES", 1)
    for operation in (workspace.scan, workspace.compute_fingerprint):
        with pytest.raises(ValueError, match="源码超过"):
            operation()
    monkeypatch.setattr(source_module, "MAX_SOURCE_BYTES", 1024)

    def fail_read(relative):
        raise OSError("读取失败")

    monkeypatch.setattr(workspace, "_read_bytes", fail_read)
    with pytest.raises(OSError, match="读取失败"):
        workspace.compute_fingerprint()
