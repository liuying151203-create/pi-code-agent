"""s17：不联网的语义编排、缓存、融合、授权和异常回归。"""

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from retrieval_helpers import FakeEmbedding, FakeZoekt, example_workspace, payload

from s16_repository_search.entities import EntityCatalog
from s16_repository_search.index import RepositoryIndex
from s17_semantic_retrieval import code as chapter
from s17_semantic_retrieval.chunks import MAX_CHUNK_CHARS, build_chunks
from s17_semantic_retrieval.embeddings import EmbeddingBatch, OpenAIEmbeddingProvider, normalized
from s17_semantic_retrieval.extension import RepositoryExtension
from s17_semantic_retrieval.retrieval import CodeRetriever
from s17_semantic_retrieval.semantic import SemanticIndex
from s17_semantic_retrieval.workspace import RepositoryWorkspace


@pytest.fixture
def components(tmp_path):
    workspace = example_workspace(tmp_path, RepositoryWorkspace)
    provider = FakeEmbedding()
    semantic = SemanticIndex(index_root=workspace.index_root, provider=provider)
    backend = FakeZoekt()
    index = RepositoryIndex(workspace=workspace, backend=backend)
    retriever = CodeRetriever(workspace=workspace, index_manager=index, semantic_index=semantic)
    return workspace, provider, backend, retriever


def test_semantic_runs_without_zoekt_and_reuses_content_cache(components):
    workspace, provider, backend, _ = components
    retriever = CodeRetriever(
        workspace=workspace,
        semantic_index=SemanticIndex(index_root=workspace.index_root, provider=provider),
    )
    first = retriever.search("负数如何归零", mode="semantic")
    assert payload(first)["status"] == "ok"
    assert payload(first)["files"][0]["path"] == "math_ops/calc.py"
    assert backend.builds == 0
    assert first.details["embedding"]["encoded_chunks"] > 0
    again = retriever.search("负数如何归零", mode="semantic")
    assert again.details["embedding"]["encoded_chunks"] == 0
    assert again.details["embedding"]["requests"] == 1
    assert again.details["embedding"]["cache_hits"] > 0
    # 缓存只含哈希/向量，不保存查询和源码正文。
    vector_path = retriever.semantic_index.path
    assert "负数".encode() not in vector_path.read_bytes()
    assert not payload(first)["search_exhaustive"]


@pytest.mark.parametrize(
    "options",
    [
        {"mode": []},
        {"mode": "semantic", "scope": "../"},
        {"limit": True},
        {"query": ""},
    ],
)
def test_invalid_queries_do_not_scan_or_send_source(components, monkeypatch, options):
    workspace, provider, _, retriever = components
    monkeypatch.setattr(workspace, "scan", Mock(side_effect=AssertionError("不能扫描")))
    arguments = {"query": "需求", **options}
    assert payload(retriever.search(**arguments))["status"] == "invalid"
    assert not provider.calls


def test_semantic_disabled_is_explicit_not_silent_text_fallback(components):
    workspace, provider, _, _ = components
    result = CodeRetriever(workspace=workspace).search("负数", mode="semantic")
    assert result.content.startswith("Error:")
    assert "未启用" in result.content and not provider.calls


def test_scope_and_deletion_exclude_cached_old_chunks(components):
    workspace, _, _, retriever = components
    first = payload(retriever.search("负数", mode="semantic", scope="math_ops"))
    assert {item["path"] for item in first["files"]} == {"math_ops/calc.py"}
    (workspace.root / "math_ops/calc.py").unlink()
    result = retriever.search("负数", mode="semantic", scope="math_ops")
    assert payload(result)["status"] == "empty"
    assert result.details["embedding"]["requests"] == 0


def test_changed_file_only_encodes_changed_chunks(components):
    workspace, _, _, retriever = components
    retriever.search("负数", mode="semantic")
    with (workspace.root / "storage.py").open("a", encoding="utf-8") as file:
        file.write("\ndef extra():\n    return 42\n")
    result = retriever.search("负数", mode="semantic")
    assert 0 < result.details["embedding"]["encoded_chunks"] < result.details["chunks"]
    assert result.details["embedding"]["cache_hits"] > 0


def test_stale_response_does_not_register_old_hashes(components, monkeypatch):
    workspace, _, _, retriever = components
    monkeypatch.setattr(workspace, "compute_fingerprint", lambda: "changed")
    result = retriever.search("负数", mode="semantic")
    assert payload(result)["status"] == "stale" and not payload(result)["files"]
    assert workspace.search_hashes == {}


@pytest.mark.parametrize("bad", [[0, 0], [float("nan")], [float("inf")], [True], [], "wrong"])
def test_bad_vectors_rejected(bad):
    with pytest.raises(ValueError):
        normalized(bad)


def test_embedding_failure_keeps_known_and_unknown_usage(components):
    workspace, provider, _, retriever = components
    original = provider.embed

    def fail_query(texts):
        if texts == ["负数"]:
            raise RuntimeError("模拟失败")
        return original(texts)

    provider.embed = fail_query
    result = retriever.search("负数", mode="semantic")
    stats = result.details["embedding"]
    assert payload(result)["status"] == "failed"
    assert stats["known_tokens"] > 0 and stats["failed_requests"] == 1
    assert stats["unknown_usage_requests"] == 1
    assert not workspace.search_hashes


def test_unknown_usage_not_forged_as_zero(components):
    _, provider, _, retriever = components
    provider.tokens = None
    stats = retriever.search("负数", mode="semantic").details["embedding"]
    assert stats["known_tokens"] == 0
    assert stats["unknown_usage_requests"] == stats["requests"]


def test_corrupt_cached_vector_does_not_trigger_replacement_request(components):
    _, provider, _, retriever = components
    retriever.search("负数", mode="semantic")
    count = len(provider.calls)
    with sqlite3.connect(retriever.semantic_index.path) as db:
        db.execute("UPDATE vectors SET value='[0,0]'")
    assert payload(retriever.search("负数", mode="semantic"))["status"] == "failed"
    assert len(provider.calls) == count


def test_dimension_change_is_explicit(components):
    _, provider, _, retriever = components
    retriever.search("负数", mode="semantic")
    provider.embed = lambda texts: EmbeddingBatch([[1, 0, 0] for _ in texts], 5)
    assert "维度" in retriever.search("负数", mode="semantic").content


def test_chunks_keep_nested_entity_ranges_and_module_code(components):
    workspace, _, _, _ = components
    snapshot = workspace.scan()
    catalog = EntityCatalog.parse(workspace.workspace_id, snapshot)
    chunks = build_chunks(snapshot, catalog)
    assert any(
        chunk.entity and chunk.entity.qualified_name.endswith("normalize") for chunk in chunks
    )
    assert any(chunk.path == "workflow.py" and chunk.entity is None for chunk in chunks)
    assert all(len(chunk.text) < MAX_CHUNK_CHARS for chunk in chunks)
    assert (
        len(
            {
                (chunk.path, line)
                for chunk in chunks
                for line in range(chunk.start_line, chunk.end_line + 1)
            }
        )
        > 30
    )


def test_adapter_reorders_response_indices_and_omits_unsupported_dimensions(monkeypatch):
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
        ],
        usage=SimpleNamespace(total_tokens=9),
    )
    monkeypatch.setattr("s17_semantic_retrieval.embeddings.OpenAI", Mock(return_value=client))
    adapter = OpenAIEmbeddingProvider(
        base_url="https://embedding.example/v1", model="test", api_key="secret"
    )
    assert adapter.embed(["a", "b"]).vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert "dimensions" not in client.embeddings.create.call_args.kwargs
    assert "secret" not in adapter.cache_key
    client.embeddings.create.return_value.data[0].index = 0
    with pytest.raises(ValueError, match="重复"):
        adapter.embed(["a", "b"])


def test_main_help_default_and_local_loop_are_independent():
    assert chapter.parse_args([]).workspace.name == "retrieval_demo"
    assert not chapter.parse_args([]).allow_remote_embedding
    tree = ast.parse(Path(chapter.__file__).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "agent_loop" for node in tree.body
    )
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0


def test_main_semantic_uses_no_chat_or_zoekt(components, monkeypatch, capsys):
    workspace, provider, _, _ = components
    monkeypatch.setattr(chapter, "HARNESS_ROOT", workspace.root.parent)
    monkeypatch.setattr(chapter.OpenAIEmbeddingProvider, "from_env", lambda: provider)
    monkeypatch.setattr(chapter, "ZoektBackend", Mock(side_effect=AssertionError("不能启动 Zoekt")))
    monkeypatch.setattr(
        chapter, "create_model_provider", Mock(side_effect=AssertionError("不能调用聊天"))
    )
    chapter.main(
        [
            "--workspace",
            str(workspace.root),
            "--search",
            "负数",
            "--search-mode",
            "semantic",
            "--allow-remote-embedding",
        ]
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "ok"
    assert "retrieval_metrics" in output.err
    assert not workspace.session_root.exists()


def test_tool_directory_only_exposes_enabled_modes(components):
    workspace, _, _, retriever = components
    api = Mock()
    extension = RepositoryExtension(
        retriever=CodeRetriever(workspace=workspace), metrics_path=workspace.trace_root / "m.jsonl"
    )
    extension.register(api)
    assert api.register_tool.call_args.args[0]["input_schema"]["properties"]["mode"]["enum"] == [
        "text",
        "symbol",
    ]
    enabled = RepositoryExtension(
        retriever=retriever, metrics_path=workspace.trace_root / "m.jsonl"
    )
    enabled.register(api)
    assert (
        "semantic"
        in api.register_tool.call_args.args[0]["input_schema"]["properties"]["mode"]["enum"]
    )


def test_embedding_index_and_query_costs_are_separate(components):
    _, _, _, retriever = components
    first = retriever.search("负数", mode="semantic").details["embedding"]
    assert first["index"]["requests"] > 0 and first["query"]["requests"] == 1
    assert first["known_tokens"] == first["index"]["known_tokens"] + first["query"]["known_tokens"]
    again = retriever.search("负数", mode="semantic").details["embedding"]
    assert again["index"]["requests"] == 0 and again["query"]["known_tokens"] == 7


def test_invalid_embedding_response_keeps_reported_cost(components, monkeypatch):
    from s17_semantic_retrieval.embeddings import EmbeddingResponseError

    _, provider, _, retriever = components

    def fail(texts):
        raise EmbeddingResponseError("维度不符", known_tokens=19)

    monkeypatch.setattr(provider, "embed", fail)
    result = retriever.search("负数", mode="semantic")
    assert payload(result)["status"] == "failed"
    assert result.details["embedding"]["known_tokens"] == 19
    assert result.details["embedding"]["unknown_usage_requests"] == 0


def test_scope_does_not_chunk_outside_oversized_line(components):
    workspace, provider, _, retriever = components
    (workspace.root / "large.txt").write_text("x" * (MAX_CHUNK_CHARS + 1), encoding="utf-8")
    scoped = retriever.search("负数", mode="semantic", scope="math_ops")
    assert payload(scoped)["status"] == "ok"
    assert all("large.txt" not in text for batch in provider.calls for text in batch)
    assert payload(retriever.search("负数", mode="semantic"))["status"] == "failed"


def test_same_model_configuration_and_revision_control_cache_identity(monkeypatch):
    monkeypatch.setattr("s17_semantic_retrieval.embeddings.OpenAI", Mock())
    config = {"base_url": "https://embedding.example/v1", "model": "model", "api_key": "first"}
    first = OpenAIEmbeddingProvider(**config)
    another_key = OpenAIEmbeddingProvider(**{**config, "api_key": "second"})
    revised = OpenAIEmbeddingProvider(**config, revision="2")
    assert first.cache_key == another_key.cache_key != revised.cache_key


def test_output_budget_file_diversity_and_model_read_fingerprint(components):
    workspace, _, _, retriever = components
    result = retriever.search("负数", mode="semantic", limit=2)
    data = payload(result)
    assert len(data["files"]) == 2
    assert all(len(file["hits"]) <= 3 for file in data["files"])
    assert len(result.content.encode()) <= 16 * 1024
    file = data["files"][0]
    with (workspace.root / file["path"]).open("a", encoding="utf-8") as output:
        output.write("\n# 新版\n")
    assert "Error:" in workspace.dispatch("read_file", {"path": file["path"]})


def test_cache_capacity_stops_before_query_encoding(components, monkeypatch):
    _, _, _, retriever = components
    monkeypatch.setattr("s17_semantic_retrieval.semantic.MAX_VECTOR_CACHE_BYTES", 1)
    result = retriever.search("负数", mode="semantic")
    assert payload(result)["status"] == "failed"
    assert result.details["embedding"]["query"]["requests"] == 0


def test_unknown_sidecar_not_overwritten_or_sent_to_provider(components):
    _, provider, _, retriever = components
    path = retriever.semantic_index.path
    path.parent.mkdir(parents=True)
    sidecar = path.with_name(path.name + "-journal")
    sidecar.write_bytes(b"unknown")
    result = retriever.search("负数", mode="semantic")
    assert payload(result)["status"] == "failed" and not provider.calls
    assert sidecar.read_bytes() == b"unknown"


def test_unrepresentable_vector_is_validation_error():
    with pytest.raises(ValueError, match="浮点"):
        normalized([10**1000])


def test_embedding_batch_size_is_enforced_and_configurable(components):
    workspace, provider, _, _ = components
    snapshot = workspace.scan()
    catalog = EntityCatalog.parse(workspace.workspace_id, snapshot)
    chunks = build_chunks(snapshot, catalog)
    semantic = SemanticIndex(index_root=workspace.index_root, provider=provider, batch_size=2)
    semantic.search(chunks, "负数", metrics={})
    assert all(len(batch) <= 2 for batch in provider.calls)
    with pytest.raises(ValueError, match="batch_size"):
        SemanticIndex(index_root=workspace.index_root, provider=provider, batch_size=0)
