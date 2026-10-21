"""s19：直接验证证据身份、片段保留、融合策略、关系补充和真实服务组装。"""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from retrieval_helpers import FakeEmbedding, FakeZoekt, example_workspace, payload

from s16_repository_search.entities import EntityCatalog
from s16_repository_search.evidence import CodeEvidence
from s16_repository_search.index import RepositoryIndex
from s16_repository_search.workspace import source_lines
from s17_semantic_retrieval.semantic import SemanticIndex
from s18_relationship_navigation.index import RelationshipIndex
from s18_relationship_navigation.retrieval import RelationshipRetriever
from s19_retrieval_fusion import code as chapter
from s19_retrieval_fusion.evidence import Candidate, EvidencePipeline, FusionPolicy
from s19_retrieval_fusion.extension import RepositoryExtension
from s19_retrieval_fusion.retrieval import CodeRetriever
from s19_retrieval_fusion.service import RepositoryRetrieval
from s19_retrieval_fusion.workspace import RepositoryWorkspace


@pytest.fixture
def components(tmp_path):
    workspace = example_workspace(tmp_path, RepositoryWorkspace)
    index = RepositoryIndex(workspace=workspace, backend=FakeZoekt())
    semantic = SemanticIndex(index_root=workspace.index_root, provider=FakeEmbedding())
    relationships = RelationshipIndex(workspace=workspace)
    pipeline = EvidencePipeline(FusionPolicy())
    search = CodeRetriever(
        workspace=workspace,
        index_manager=index,
        semantic_index=semantic,
        evidence_pipeline=pipeline,
        relationship_index=relationships,
    )
    navigation = RelationshipRetriever(workspace=workspace, relationship_index=relationships)
    service = RepositoryRetrieval(search_retriever=search, relationship_retriever=navigation)
    return workspace, pipeline, search, service


def candidates_for(workspace):
    snapshot = workspace.scan()
    catalog = EntityCatalog.parse(workspace.workspace_id, snapshot)
    entity = next(e for e in catalog.entities if e.qualified_name == "Calculator.add")
    lines = source_lines(snapshot.file_contents[entity.path])
    first = CodeEvidence(entity.path, 8, 8, lines[7], entity.content_hash, "symbol", entity)
    last = CodeEvidence(entity.path, 19, 19, lines[18], entity.content_hash, "semantic", entity)
    return snapshot, first, last


def test_entity_merge_preserves_distinct_spans_and_every_source(components):
    workspace, pipeline, _, _ = components
    snapshot, first, last = candidates_for(workspace)
    text_first = replace(first, source="text")
    ranked = pipeline.rank(
        {
            "symbol": [Candidate(first), Candidate(first)],
            "text": [Candidate(text_first)],
            "semantic": [Candidate(last, 0.9)],
        }
    )
    assert len(ranked) == 1
    assert ranked[0].channel_ranks == {"symbol": 1, "text": 1, "semantic": 1}
    data = pipeline.pack({"status": "ok", "truncated": False}, ranked, snapshot, limit=10)
    hits = data["files"][0]["hits"]
    assert [hit["start_line"] for hit in hits] == [8, 19]
    assert hits[0]["sources"] == ["symbol", "text"]
    assert hits[1]["similarities"] == {"semantic": 0.9}
    assert first.source == "symbol"  # 输入不可变，不把一次查询的排名写回缓存候选。


def test_same_channel_multiple_chunks_count_once_but_are_not_discarded(components):
    workspace, pipeline, _, _ = components
    _, first, last = candidates_for(workspace)
    ranked = pipeline.rank(
        {"semantic": [Candidate(replace(first, source="semantic")), Candidate(last)]}
    )
    assert len(ranked) == 1 and len(ranked[0].matches) == 2
    assert ranked[0].channel_ranks == {"semantic": 1}


def test_different_file_versions_cannot_be_fused(components):
    workspace, pipeline, _, _ = components
    _, first, _ = candidates_for(workspace)
    with pytest.raises(ValueError, match="不同版本"):
        pipeline.rank(
            {
                "symbol": [Candidate(first)],
                "text": [Candidate(replace(first, source="text", content_hash="old"))],
            }
        )


@pytest.mark.parametrize(
    "change",
    [{"start_line": 0}, {"end_line": 9999}, {"content_hash": "old"}, {"path": "missing.py"}],
)
def test_pack_rejects_evidence_outside_snapshot(components, change):
    workspace, pipeline, _, _ = components
    snapshot, first, _ = candidates_for(workspace)
    ranked = pipeline.rank({"symbol": [Candidate(replace(first, **change))]})
    with pytest.raises(ValueError, match="证据"):
        pipeline.pack({"truncated": False}, ranked, snapshot, limit=10)


def test_weights_change_rank_without_comparing_raw_engine_scores(components):
    workspace, _, _, _ = components
    _, first, last = candidates_for(workspace)
    other = replace(last, path="storage.py", entity=None, content_hash="another")
    pipeline = EvidencePipeline(FusionPolicy(weights={"text": 1, "symbol": 1, "semantic": 2}))
    ranked = pipeline.rank({"symbol": [Candidate(first)], "semantic": [Candidate(other, 0.01)]})
    assert ranked[0].matches[0].evidence.path == "storage.py"


@pytest.mark.parametrize(
    "options",
    [
        {"rrf_k": 0},
        {"rrf_k": True},
        {"hits_per_file": 0},
        {"snippet_chars": 0},
        {"weights": {"text": 1}},
        {"weights": {"text": 1, "symbol": 1, "semantic": float("nan")}},
    ],
)
def test_invalid_policy_fails_before_retrieval(options):
    with pytest.raises(ValueError):
        FusionPolicy(**options)


def test_relations_only_use_remaining_budget_and_omissions_are_visible(components):
    workspace, pipeline, _, _ = components
    snapshot, first, _ = candidates_for(workspace)
    ranked = pipeline.rank({"symbol": [Candidate(first)]})
    data = pipeline.pack(
        {"status": "ok", "truncated": False},
        ranked,
        snapshot,
        limit=10,
        relations=[{"evidence": "中" * 10000}],
    )
    assert data["files"] and not data["related_context"]
    assert data["omissions"]["byte_budget"] == 1 and data["truncated"]
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= 16 * 1024


def test_relation_expansion_reuses_snapshot_and_catalog_without_embedding(components, monkeypatch):
    workspace, _, search, _ = components
    scan = Mock(wraps=workspace.scan)
    prepare = Mock(wraps=search.relationship_index.prepare)
    monkeypatch.setattr(workspace, "scan", scan)
    monkeypatch.setattr(search.relationship_index, "prepare", prepare)
    result = search.search("add", mode="symbol", include_related=True)
    data = payload(result)
    assert data["status"] == "ok" and data["related_context"]
    assert scan.call_count == 1
    assert prepare.call_args.kwargs["catalog"].fingerprint == data["source_fingerprint"]
    assert result.details["embedding"] == {}
    assert all("rrf_score" not in edge for edge in data["related_context"])
    assert {"checkout", "preview"} <= {
        edge["source"]["qualified_name"] for edge in data["related_context"]
    }
    assert "workflow.py" in workspace.search_hashes


def test_related_scope_and_final_version_check_apply_to_all_evidence(components, monkeypatch):
    workspace, _, search, _ = components
    data = payload(search.search("add", mode="symbol", scope="math_ops", include_related=True))
    assert data["status"] == "ok" and not data["related_context"]
    workspace.search_hashes.clear()
    monkeypatch.setattr(workspace, "compute_fingerprint", lambda: "changed")
    stale = payload(search.search("add", mode="symbol", include_related=True))
    assert stale["status"] == "stale" and not stale["files"] and not stale["related_context"]
    assert not workspace.search_hashes


def test_keyword_order_does_not_change_rank_or_evidence(components):
    _, _, search, _ = components
    one = payload(search.search("负数", mode="hybrid", keywords=["normalize", "add"]))
    two = payload(search.search("负数", mode="hybrid", keywords=["add", "normalize"]))
    assert one["files"] == two["files"]
    assert one["coverage"] == two["coverage"]


def test_service_and_both_tool_handlers_share_components(components):
    workspace, _, _, service = components
    api = Mock()
    extension = RepositoryExtension(
        retriever=service, metrics_path=workspace.trace_root / "m.jsonl"
    )
    extension.register(api)
    tools = {call.args[0]["name"]: call.args[0] for call in api.register_tool.call_args_list}
    assert set(tools) == {"search_code", "related_code"}
    assert "include_related" in tools["search_code"]["input_schema"]["properties"]
    found = payload(extension.handle_search({"query": "add", "mode": "symbol"}))
    entity = next(
        hit["entity"]
        for file in found["files"]
        for hit in file["hits"]
        if hit["entity"]["qualified_name"] == "Calculator.add"
    )
    related = payload(extension.handle_related({"entity_id": entity["entity_id"]}))
    assert len(related["relations"]) == 2
    assert service.workspace is workspace
    assert not extension.handle_related({"unknown": "x"}).details.get("embedding")


def test_service_rejects_different_workspaces(components):
    _, _, search, _service = components
    wrong = Mock(workspace=object())
    with pytest.raises(ValueError, match="workspace"):
        RepositoryRetrieval(search_retriever=search, relationship_retriever=wrong)


def test_cli_only_accepts_relation_expansion_for_search():
    assert chapter.parse_args(["--search", "add", "--include-related"]).include_related
    with pytest.raises(SystemExit):
        chapter.parse_args(["--include-related"])


def test_relation_scope_is_filtered_before_neighbor_limit(components):
    workspace, _, search, _ = components
    scoped = workspace.root / "z_scope"
    scoped.mkdir()
    (scoped / "ops.py").write_text("def scoped_add(a, b):\n    return a + b\n", encoding="utf-8")
    calls = "from z_scope.ops import scoped_add\n" + "".join(
        f"def outside_{i}():\n    return scoped_add(1, 2)\n" for i in range(12)
    )
    (workspace.root / "a_outside.py").write_text(calls, encoding="utf-8")
    (scoped / "caller.py").write_text(
        "from z_scope.ops import scoped_add\ndef caller():\n    return scoped_add(1, 2)\n",
        encoding="utf-8",
    )
    data = payload(
        search.search("scoped_add", mode="symbol", scope="z_scope", include_related=True)
    )
    assert data["status"] == "ok"
    assert [edge["path"] for edge in data["related_context"]] == ["z_scope/caller.py"]


def test_fusion_policy_copies_and_freezes_weights():
    weights = {"text": 1.0, "symbol": 1.0, "semantic": 1.0}
    policy = FusionPolicy(weights=weights)
    weights["semantic"] = 9.0
    assert policy.weights["semantic"] == 1.0
    with pytest.raises(TypeError):
        policy.weights["semantic"] = 2.0


def test_direct_navigation_does_not_advertise_unconfigured_search_expansion(components):
    workspace, _, search, service = components
    search.relationship_index = None
    limited_service = RepositoryRetrieval(
        search_retriever=search, relationship_retriever=service.relationship_retriever
    )
    api = Mock()
    RepositoryExtension(
        retriever=limited_service, metrics_path=workspace.trace_root / "m.jsonl"
    ).register(api)
    tools = {call.args[0]["name"]: call.args[0] for call in api.register_tool.call_args_list}
    assert "related_code" in tools
    assert "include_related" not in tools["search_code"]["input_schema"]["properties"]
