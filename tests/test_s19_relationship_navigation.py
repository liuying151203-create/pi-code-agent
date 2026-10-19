"""s19：关系证据、作用域、持久化、版本和工具接入的离线回归。"""

import ast
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from retrieval_helpers import FakeZoekt, example_workspace, payload

from s11_runtime_observability.code import AgentEvent
from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.session import SessionManager
from s17_repository_symbols.entities import EntityCatalog
from s17_repository_symbols.index import RepositoryIndex
from s18_semantic_retrieval.extension import RepositoryExtension as SearchExtension
from s18_semantic_retrieval.retrieval import CodeRetriever
from s19_relationship_navigation import code as chapter
from s19_relationship_navigation.extension import RepositoryExtension
from s19_relationship_navigation.index import RelationshipIndex
from s19_relationship_navigation.relations import RelationshipGraph
from s19_relationship_navigation.retrieval import RelationshipRetriever
from s19_relationship_navigation.workspace import RepositoryWorkspace


@pytest.fixture
def components(tmp_path):
    workspace = example_workspace(tmp_path, RepositoryWorkspace)
    index = RelationshipIndex(workspace=workspace)
    retriever = RelationshipRetriever(workspace=workspace, relationship_index=index)
    return workspace, index, retriever


def graph_for(workspace):
    snapshot = workspace.scan()
    catalog = EntityCatalog.parse(workspace.workspace_id, snapshot)
    return RelationshipGraph.build(snapshot, catalog)


def entity_id(graph, name, path=None):
    return next(
        key
        for key, node in graph.nodes.items()
        if node["qualified_name"] == name and (path is None or node["path"] == path)
    )


def test_example_distinguishes_same_name_and_resolves_direct_alias_calls(components):
    workspace, _, _ = components
    graph = graph_for(workspace)
    calc_add = entity_id(graph, "Calculator.add")
    store_add = entity_id(graph, "Store.add")
    edges, _ = graph.neighbors(calc_add, direction="incoming", kind="calls", limit=20)
    assert {(graph.nodes[edge.source_id]["qualified_name"], edge.line) for edge in edges} == {
        ("checkout", 13),
        ("preview", 25),
    }
    store_edges, _ = graph.neighbors(store_add, direction="incoming", kind="calls", limit=20)
    assert [edge.expression for edge in store_edges] == ["Store().add"]
    normalize = entity_id(graph, "Calculator.add.normalize")
    calls, _ = graph.neighbors(normalize, direction="incoming", kind="calls", limit=20)
    assert len(calls) == 2 and all(edge.source_id == calc_add for edge in calls)


def test_relationship_cache_reuses_without_analyzing_again(components, monkeypatch):
    workspace, index, _ = components
    snapshot = workspace.scan()
    first, rebuilt = index.prepare(snapshot)
    assert rebuilt
    monkeypatch.setattr(RelationshipGraph, "build", Mock(side_effect=AssertionError("不重建")))
    again, rebuilt = index.prepare(snapshot)
    assert not rebuilt
    assert first.nodes == again.nodes and first.relations == again.relations
    files = list(workspace.index_root.rglob("*"))
    assert not any(path.name == "source" for path in files)


def test_current_search_entity_id_drives_related_tool_and_hash_checked_read(components):
    workspace, _, retriever = components
    search = CodeRetriever(
        workspace=workspace, index_manager=RepositoryIndex(workspace=workspace, backend=FakeZoekt())
    )
    found = payload(search.search("add", mode="symbol"))
    calc = next(
        hit["entity"]
        for file in found["files"]
        for hit in file["hits"]
        if hit["entity"]["qualified_name"] == "Calculator.add"
    )
    result = payload(retriever.related(calc["entity_id"]))
    assert result["status"] == "ok"
    assert len(result["relations"]) == 2
    assert "workflow.py" in workspace.search_hashes
    with (workspace.root / "workflow.py").open("a", encoding="utf-8") as file:
        file.write("\n# 修改\n")
    assert "Error:" in workspace.dispatch("read_file", {"path": "workflow.py"})


def test_modified_entity_id_cannot_query_new_graph(components):
    workspace, _, retriever = components
    graph = graph_for(workspace)
    old = entity_id(graph, "Calculator.add")
    with (workspace.root / "math_ops/calc.py").open("a", encoding="utf-8") as file:
        file.write("\n# 新版本\n")
    result = retriever.related(old)
    assert result.content.startswith("Error:") and "过期" in result.content


@pytest.mark.parametrize(
    "source,expression",
    [
        ("def test(f):\n    return f()\n", "f"),
        (
            "from math_ops.calc import Calculator\ndef test(obj):\n    return obj.add(1,2)\n",
            "obj.add",
        ),
        (
            "from math_ops.calc import Calculator\ndef test():\n    op=Calculator.add\n    op=unknown\n    return op()\n",
            "op",
        ),
        (
            "from math_ops.calc import Calculator\ndef test(Calculator):\n    return Calculator.add()\n",
            "Calculator.add",
        ),
        (
            "from math_ops.calc import Calculator\ndef test():\n    Calculator=unknown\n    return Calculator.add()\n",
            "Calculator.add",
        ),
    ],
)
def test_dynamic_and_shadowed_calls_are_unresolved(components, source, expression):
    workspace, _, _ = components
    (workspace.root / "ambiguous.py").write_text(source, encoding="utf-8")
    graph = graph_for(workspace)
    edges = [
        edge
        for edge in graph.relations
        if edge.path == "ambiguous.py" and edge.expression == expression
    ]
    assert edges and all(edge.target_id is None and edge.reason for edge in edges)


def test_import_inheritance_instance_alias_and_recursive_cycle(components):
    workspace, _, retriever = components
    (workspace.root / "extra.py").write_text(
        "from storage import Store as S\nclass Child(S):\n    pass\n"
        "def recurse():\n    return recurse()\n"
        "def use():\n    store=S()\n    operation=store.add\n    return operation(1)\n",
        encoding="utf-8",
    )
    graph = graph_for(workspace)
    store_id = entity_id(graph, "Store")
    assert any(edge.kind == "inherits" and edge.target_id == store_id for edge in graph.relations)
    assert any(edge.kind == "imports" and edge.target_id == store_id for edge in graph.relations)
    assert any(
        edge.expression == "operation" and edge.target_id == entity_id(graph, "Store.add")
        for edge in graph.relations
    )
    recursive_id = entity_id(graph, "recurse")
    result = payload(retriever.related(recursive_id, direction="outgoing"))
    assert len(result["relations"]) == 1
    assert result["relations"][0]["target_id"] == recursive_id


def test_unknown_external_imports_not_guessed_as_local_names(components):
    workspace, _, _ = components
    (workspace.root / "outside.py").write_text(
        "from imaginary import Store\nStore().add()\n", encoding="utf-8"
    )
    graph = graph_for(workspace)
    assert all(edge.target_id is None for edge in graph.relations if edge.path == "outside.py")


def test_stale_graph_does_not_register_hashes(components, monkeypatch):
    workspace, _, retriever = components
    graph = graph_for(workspace)
    monkeypatch.setattr(workspace, "compute_fingerprint", lambda: "changed")
    result = payload(retriever.related(entity_id(graph, "Calculator.add")))
    assert result["status"] == "stale" and not result["relations"]
    assert workspace.search_hashes == {}


@pytest.mark.parametrize(
    "options",
    [
        {"entity_id": "old"},
        {"direction": "sideways"},
        {"kind": "random"},
        {"limit": True},
        {"limit": 21},
    ],
)
def test_invalid_input_rejected_before_scan(components, monkeypatch, options):
    workspace, _, retriever = components
    monkeypatch.setattr(workspace, "scan", Mock(side_effect=AssertionError("不能扫描")))
    assert payload(retriever.related(**{"entity_id": "a" * 64, **options}))["status"] == "invalid"


def test_bounded_output_with_unresolved_and_truncation(components):
    workspace, _, retriever = components
    (workspace.root / "many.py").write_text(
        "def caller():\n" + "    unknown()\n" * 40, encoding="utf-8"
    )
    graph = graph_for(workspace)
    result = retriever.related(entity_id(graph, "caller"), direction="outgoing", limit=3)
    data = payload(result)
    assert len(data["relations"]) == 3 and data["truncated"]
    assert all(item["target"] is None and item["evidence"] for item in data["relations"])
    assert len(result.content.encode()) <= 16 * 1024


def test_corrupt_graph_metadata_fails_not_empty(components):
    workspace, index, retriever = components
    graph, _ = index.prepare(workspace.scan())
    database = next(workspace.index_root.rglob("relations.sqlite"))
    with sqlite3.connect(database) as db:
        db.execute("UPDATE metadata SET value='{}'")
    result = retriever.related(entity_id(graph, "Calculator.add"))
    assert payload(result)["status"] == "failed"
    assert "缓存" in result.content


def test_real_registry_has_search_related_and_read_only_tools(components):
    workspace, _, retriever = components
    search = CodeRetriever(
        workspace=workspace, index_manager=RepositoryIndex(workspace=workspace, backend=FakeZoekt())
    )
    metrics_path = workspace.trace_root / "metrics.jsonl"
    search_extension = SearchExtension(retriever=search, metrics_path=metrics_path)
    extension = RepositoryExtension(
        search_extension=search_extension, relationship_retriever=retriever
    )
    session = SessionManager.open(workspace.session_root / "test.jsonl")
    api = ExtensionAPI(
        base_tools=workspace.tools,
        base_dispatch=workspace.dispatch,
        permission_hook=workspace.permission_hook,
        session=session,
    )
    extension.register(api)
    assert {tool["name"] for tool in api.tools} == {
        "read_file",
        "grep",
        "glob",
        "search_code",
        "related_code",
    }
    found = payload(api.dispatch("search_code", {"query": "add", "mode": "symbol"}))
    entity = next(hit["entity"] for file in found["files"] for hit in file["hits"] if hit["entity"])
    result = api.dispatch("related_code", {"entity_id": entity["entity_id"]})
    assert payload(result)["status"] == "ok"
    extension.record_metrics(
        AgentEvent(
            type="tool_result",
            data={"name": "related_code", "tool_call_id": "r1", "details": result.details},
        )
    )
    record = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert record["mode"] == "relations" and record["tool_call_id"] == "r1"
    assert "evidence" not in record


def test_main_related_needs_no_zoekt_or_embedding(components, monkeypatch, capsys):
    workspace, _, _ = components
    graph = graph_for(workspace)
    monkeypatch.setattr(chapter, "HARNESS_ROOT", workspace.root.parent)
    monkeypatch.setattr(chapter, "ZoektBackend", Mock(side_effect=AssertionError("不能启动 Zoekt")))
    monkeypatch.setattr(
        chapter.OpenAIEmbeddingProvider,
        "from_env",
        Mock(side_effect=AssertionError("不能调用 embedding")),
    )
    chapter.main(
        ["--workspace", str(workspace.root), "--related", entity_id(graph, "Calculator.add")]
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert not workspace.session_root.exists()


def test_chapter_paths_and_local_loop_remain_independent(components):
    workspace, _, _ = components
    assert workspace.index_root == workspace.root / ".indexes/s19"
    assert workspace.session_root.parts[-2] == "s19"
    assert chapter.parse_args([]).workspace.name == "retrieval_demo"
    tree = ast.parse(Path(chapter.__file__).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "agent_loop" for node in tree.body
    )


@pytest.mark.parametrize(
    "source,expression",
    [
        (
            "from math_ops.calc import Calculator\ndef f(flag):\n    if flag:\n        op=Calculator.add\n    return op()\n",
            "op",
        ),
        (
            "from math_ops.calc import Calculator\ndef f():\n    return (lambda Calculator: Calculator.add())(unknown)\n",
            "Calculator.add",
        ),
        (
            "from math_ops.calc import Calculator\ndef f(items):\n    return [Calculator.add() for Calculator in items]\n",
            "Calculator.add",
        ),
        ("class C:\n    def add(): pass\n    def add(): pass\nC.add()\n", "C.add"),
        ("class C:\n    def add(): pass\n    add=unknown\nC.add()\n", "C.add"),
    ],
)
def test_conditional_anonymous_and_overwritten_members_not_guessed(components, source, expression):
    workspace, _, _ = components
    (workspace.root / "edge_case.py").write_text(source, encoding="utf-8")
    graph = graph_for(workspace)
    edges = [
        edge
        for edge in graph.relations
        if edge.path == "edge_case.py" and edge.expression == expression
    ]
    assert edges and all(edge.target_id is None for edge in edges)


def test_module_export_reassignment_is_not_resolved_to_old_definition(components):
    workspace, _, _ = components
    (workspace.root / "provider.py").write_text("def run(): pass\nrun=unknown\n", encoding="utf-8")
    (workspace.root / "consumer.py").write_text(
        "from provider import run\nrun()\n", encoding="utf-8"
    )
    graph = graph_for(workspace)
    assert all(edge.target_id is None for edge in graph.relations if edge.path == "consumer.py")


def test_relative_imports_resolve_within_package_but_not_above_root(components):
    workspace, _, _ = components
    (workspace.root / "math_ops/use.py").write_text(
        "from .calc import Calculator\nCalculator.add(1,2)\n", encoding="utf-8"
    )
    (workspace.root / "bad_relative.py").write_text(
        "from .storage import Store\nStore().add()\n", encoding="utf-8"
    )
    graph = graph_for(workspace)
    assert any(
        edge.path == "math_ops/use.py" and edge.target_id == entity_id(graph, "Calculator.add")
        for edge in graph.relations
    )
    assert all(edge.target_id is None for edge in graph.relations if edge.path == "bad_relative.py")


def test_graph_cache_rejects_corrupt_evidence_line(components):
    workspace, index, retriever = components
    graph, _ = index.prepare(workspace.scan())
    database = next(workspace.index_root.rglob("relations.sqlite"))
    with sqlite3.connect(database) as db:
        record = json.loads(db.execute("SELECT value FROM relations LIMIT 1").fetchone()[0])
        record["line"] = 1000000
        db.execute("UPDATE relations SET value=? WHERE rowid=1", (json.dumps(record),))
    assert payload(retriever.related(entity_id(graph, "Calculator.add")))["status"] == "failed"


def test_extreme_entity_metadata_cannot_bypass_output_budget(components):
    workspace, _, retriever = components
    name = "f" * 18000
    (workspace.root / "huge.py").write_text(f"def {name}():\n    pass\n", encoding="utf-8")
    graph = graph_for(workspace)
    result = retriever.related(entity_id(graph, name), direction="outgoing")
    assert payload(result)["status"] == "failed"
    assert len(result.content.encode()) < 16 * 1024


def test_graph_cache_rejects_missing_module_node(components):
    workspace, index, retriever = components
    graph, _ = index.prepare(workspace.scan())
    module_id = next(key for key, node in graph.nodes.items() if node["kind"] == "module")
    database = next(workspace.index_root.rglob("relations.sqlite"))
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM nodes WHERE id=?", (module_id,))
    assert payload(retriever.related(entity_id(graph, "Calculator.add")))["status"] == "failed"
