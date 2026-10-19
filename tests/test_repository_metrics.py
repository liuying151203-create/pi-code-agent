"""s16/s17 容量指标回归：离线后端不证明真实 Zoekt 性能。"""

import json
import os

import pytest

from s16_repository_search import cache as cache_module
from s16_repository_search import code as text_chapter
from s17_repository_symbols import code as symbol_chapter


class Backend:
    """只提供编排所需的离线分片和位置，不模拟检索算法。"""

    cache_key = "metrics-test-image"

    def check_ready(self):
        pass

    def build_index(self, source_copy_dir, index_dir):
        (index_dir / "fake.zoekt").write_bytes(b"test-index")

    def search_index(self, index_dir, query, scope, limit, **kwargs):
        return {"files": [{"path": "app.py", "lines": [1]}], "exhaustive": True, "truncated": False}


@pytest.fixture(params=[text_chapter, symbol_chapter], ids=["s16", "s17"])
def components(request, tmp_path):
    target = tmp_path / "target"
    harness = tmp_path / "harness"
    target.mkdir()
    harness.mkdir()
    (target / "app.py").write_text("def add():\n    return '中文'\n", encoding="utf-8")
    chapter = request.param
    workspace = chapter.RepositoryWorkspace(target, harness)
    index = chapter.RepositoryIndex(workspace=workspace, backend=Backend())
    retriever = chapter.CodeRetriever(workspace=workspace, index_manager=index)
    return chapter, workspace, retriever, harness


def test_cold_and_reused_metrics_measure_source_and_current_generation_only(components):
    chapter, workspace, retriever, _ = components
    first = retriever.search("add")
    assert first.details["status"] == "ok" and first.details["rebuilt"] is True
    generation = workspace.index_root / first.details["generation"]
    expected_bytes = sum(path.stat().st_size for path in generation.rglob("*") if path.is_file())
    assert first.details["cache_bytes"] == expected_bytes
    assert first.details["source_bytes"] == len((workspace.root / "app.py").read_bytes())
    assert first.details["cache_stat_ms"] >= 0
    # 其他代次或未知目录不属于本次查询容量，不应被逐查询统计遍历。
    (workspace.index_root / "unknown").mkdir()
    (workspace.index_root / "unknown" / "old.bin").write_bytes(b"x" * 1000)
    second = retriever.search("add")
    assert second.details["rebuilt"] is False
    assert second.details["cache_bytes"] == expected_bytes
    assert second.details["generation"] == first.details["generation"]
    if chapter is symbol_chapter:
        assert (generation / "entities.sqlite").is_file()
    assert "cache_bytes" not in json.loads(first.content)


def test_invalid_request_has_unknown_capacity_instead_of_fabricated_zero(components):
    _, _, retriever, _ = components
    result = retriever.search("")
    assert result.details["status"] == "invalid"
    for field in ("source_bytes", "cache_bytes", "cache_stat_ms"):
        assert result.details[field] is None


def test_direct_entry_sends_metrics_to_stderr_without_creating_session(
    components, monkeypatch, capsys
):
    chapter, workspace, _, harness = components
    monkeypatch.setattr(chapter, "HARNESS_ROOT", harness)
    monkeypatch.setattr(chapter, "ZoektBackend", lambda _: Backend())
    chapter.main(["--workspace", str(workspace.root), "--search", "add"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "ok"
    assert json.loads(captured.err)["retrieval_metrics"]["source_bytes"] > 0
    assert not workspace.session_root.exists() and not workspace.trace_root.exists()


def test_cache_measurement_rejects_hardlinks_and_excess_entries(tmp_path, monkeypatch):
    directory = tmp_path / "cache"
    directory.mkdir()
    source = tmp_path / "outside"
    source.write_bytes(b"outside")
    os.link(source, directory / "alias")
    with pytest.raises(ValueError, match="硬链接"):
        cache_module.cache_directory_bytes(directory)
    (directory / "alias").unlink()
    (directory / "normal").write_bytes(b"abc")
    monkeypatch.setattr(cache_module, "MAX_CACHE_ENTRIES", 0)
    with pytest.raises(ValueError, match="目录项"):
        cache_module.cache_directory_bytes(directory)
