"""s22：标签隔离、固定分母、候选排序与实际会话成本，全部离线。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from retrieval_helpers import FakeEmbedding, FakeZoekt, example_workspace

from s11_runtime_observability import code as runtime
from s15_stateful_extensions.result import ToolResult
from s15_stateful_extensions.session import SessionManager
from s16_repository_search.index import RepositoryIndex
from s17_semantic_retrieval.semantic import SemanticIndex
from s18_relationship_navigation.index import RelationshipIndex
from s18_relationship_navigation.retrieval import RelationshipRetriever
from s19_retrieval_fusion.retrieval import CodeRetriever
from s22_retrieval_evaluation import code as chapter
from s22_retrieval_evaluation.dataset import EvaluationCase, SearchTask, load_cases
from s22_retrieval_evaluation.evaluation import EvaluationRunner, file_metrics
from s22_retrieval_evaluation.session_stats import read_session_stats
from s22_retrieval_evaluation.strategies import MODES, ComponentLocator, RetrievalRun, fuse_files
from s22_retrieval_evaluation.workspace import RepositoryWorkspace


@pytest.fixture
def components(tmp_path):
    workspace = example_workspace(tmp_path, RepositoryWorkspace)
    fingerprint = workspace.scan().fingerprint
    row = {
        "id": "normalize",
        "query": "负数如何归零",
        "keywords": ["normalize"],
        "symbol_query": "normalize",
        "source_fingerprint": fingerprint,
        "gold_files": ["math_ops/calc.py"],
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    retriever = CodeRetriever(
        workspace=workspace,
        index_manager=RepositoryIndex(workspace=workspace, backend=FakeZoekt()),
        semantic_index=SemanticIndex(index_root=workspace.index_root, provider=FakeEmbedding()),
    )
    related = RelationshipRetriever(
        workspace=workspace, relationship_index=RelationshipIndex(workspace=workspace)
    )
    return workspace, dataset, row, retriever, related


@pytest.mark.parametrize("mode", MODES)
def test_real_component_pipeline_scores_blind_tasks(components, mode):
    workspace, dataset, _, retriever, related = components
    cases = load_cases(dataset, workspace=workspace)
    locator = ComponentLocator(
        workspace=workspace, mode=mode, retriever=retriever, relationship_retriever=related
    )
    report = EvaluationRunner(workspace=workspace, locator=locator).run(cases)
    assert report["layer"] == "component" and report["failed_cases"] == 0
    assert report["averages"]["file_recall_at_10"] == 1
    assert not hasattr(cases[0].task, "gold_files")
    assert report["cases"][0]["logical_tool_calls"] >= 1


def test_relationship_expansion_uses_explicit_task_seed_not_gold(components):
    workspace, dataset, row, retriever, related = components
    row.update(
        id="callers",
        query="谁调用 Calculator.add",
        keywords=["Calculator.add"],
        symbol_query="add",
        seed={"query": "add", "qualified_name": "Calculator.add"},
        gold_files=["workflow.py"],
    )
    dataset.write_text(json.dumps(row), encoding="utf-8")
    cases = load_cases(dataset, workspace=workspace)
    locator = ComponentLocator(
        workspace=workspace,
        mode="hybrid-relations",
        retriever=retriever,
        relationship_retriever=related,
    )
    report = EvaluationRunner(workspace=workspace, locator=locator).run(cases)
    assert report["averages"]["file_recall_at_10"] == 1
    assert report["cases"][0]["logical_tool_calls"] == 3


@pytest.mark.parametrize(
    "change",
    [
        {"gold_files": []},
        {"gold_files": ["new_file.py"]},
        {"gold_files": [".env"]},
        {"gold_files": ["storage.py", "storage.py"]},
        {"source_fingerprint": "changed"},
        {"keywords": []},
        {"keywords": [False]},
        {"id": "../../escape"},
        {"seed": {"query": "add", "qualified_name": "Calculator.add", "path": "gold.py"}},
    ],
)
def test_invalid_labels_or_inputs_rejected_before_queries(components, change):
    workspace, dataset, row, _, _ = components
    row.update(change)
    dataset.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        load_cases(dataset, workspace=workspace)


def test_gold_cannot_be_inside_indexed_target(components):
    workspace, _, row, _, _ = components
    dataset = workspace.root / "answers.jsonl"
    dataset.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="之外"):
        load_cases(dataset, workspace=workspace)


def test_duplicate_tasks_are_not_silently_dropped(components):
    workspace, dataset, row, _, _ = components
    dataset.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_cases(dataset, workspace=workspace)


def test_failed_and_empty_cases_stay_in_macro_average(components):
    workspace, _, _, _, _ = components
    fingerprint = workspace.scan().fingerprint
    cases = [
        EvaluationCase(
            SearchTask(str(index), "query", ("term",), "term", None),
            fingerprint,
            frozenset({"math_ops/calc.py"}),
        )
        for index in range(3)
    ]
    results = iter(
        [
            RetrievalRun(files=["math_ops/calc.py"]),
            RetrievalRun(files=[]),
            RetrievalRun(status="failed", error="timeout", logical_tool_calls=1),
        ]
    )
    locator = SimpleNamespace(workspace=workspace, mode="text", locate=lambda task: next(results))
    report = EvaluationRunner(workspace=workspace, locator=locator).run(cases)
    assert report["case_count"] == 3 and report["failed_cases"] == 1
    assert report["averages"]["file_recall_at_10"] == pytest.approx(1 / 3)


def test_file_recall_is_not_any_hit_rate_and_duplicates_do_not_inflate():
    result = file_metrics(["a.py", "a.py", "c.py"], frozenset({"a.py", "b.py"}))
    assert result["file_recall_at_10"] == 0.5 and result["hit_at_10"] == 1
    assert result["reciprocal_rank_at_10"] == 1
    with pytest.raises(ValueError):
        file_metrics([], frozenset())


def test_changed_source_is_failure_not_new_dataset(components):
    workspace, dataset, _, retriever, _ = components
    cases = load_cases(dataset, workspace=workspace)
    (workspace.root / "new.py").write_text("x=1\n", encoding="utf-8")
    locator = ComponentLocator(workspace=workspace, mode="text", retriever=retriever)
    report = EvaluationRunner(workspace=workspace, locator=locator).run(cases)
    assert report["failed_cases"] == 1 and report["averages"]["file_recall_at_10"] == 0


def test_failed_embedding_is_not_downgraded_and_metrics_retained(components):
    workspace, dataset, _, retriever, _ = components
    retriever.semantic_index.provider.embed = Mock(side_effect=RuntimeError("offline failure"))
    cases = load_cases(dataset, workspace=workspace)
    locator = ComponentLocator(workspace=workspace, mode="hybrid", retriever=retriever)
    report = EvaluationRunner(workspace=workspace, locator=locator).run(cases)
    assert report["failed_cases"] == 1
    assert report["cases"][0]["retrieval_metrics"][0]["embedding"]["unknown_usage_requests"] == 1


def test_file_fusion_uses_rank_not_number_of_duplicate_hits():
    assert fuse_files([["a.py", "a.py", "b.py"], ["b.py"]])[0] == "b.py"


def test_session_costs_use_existing_entries_and_never_write_log(tmp_path):
    path = tmp_path / "session.jsonl"
    session = SessionManager.open(path)
    session.append_message({"role": "user", "content": "一个任务"})
    session.append_assistant(
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "s1", "name": "search_code", "input": {"query": "x"}}
            ],
        },
        runtime.ModelResponse(
            content=[],
            model="offline-test",
            stop_reason="tool_use",
            usage=runtime.ModelUsage(input_tokens=5, output_tokens=3),
        ),
    )
    session.append_tool_results(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "s1", "content": "{}"}],
        },
        {
            "s1": {
                "name": "search_code",
                "details": {
                    "embedding": {"requests": 2, "known_tokens": 7, "unknown_usage_requests": 1}
                },
            }
        },
    )
    session.append_assistant(
        {"role": "assistant", "content": [{"type": "text", "text": "失败"}]},
        runtime.ModelResponse(
            content=[], model="offline-test", stop_reason="error", usage=runtime.ModelUsage()
        ),
    )
    before = path.read_bytes()
    report = read_session_stats(path)
    assert report["chat"]["tool_calls"] == 1
    assert report["chat"]["known_tokens"] == 8 and report["chat"]["unknown_usage_requests"] == 1
    assert report["chat"]["failed_requests"] == 1
    assert (
        report["embedding"]["known_tokens"] == 7
        and report["embedding"]["unknown_usage_requests"] == 1
    )
    assert report["scope"] == "entire_session" and path.read_bytes() == before


def test_basic_cli_uses_no_docker_or_model_and_refuses_overwrite(
    components, monkeypatch, capsys, tmp_path
):
    workspace, dataset, _, _, _ = components
    output = tmp_path / "result.json"
    monkeypatch.setattr(chapter, "HARNESS_ROOT", workspace.root.parent)
    monkeypatch.setattr(
        chapter, "ZoektBackend", Mock(side_effect=AssertionError("不能启动 Docker"))
    )
    chapter.main(
        ["--workspace", str(workspace.root), "--dataset", str(dataset), "--output", str(output)]
    )
    assert json.loads(capsys.readouterr().out)["failed_cases"] == 0
    with pytest.raises(FileExistsError):
        chapter.main(
            ["--workspace", str(workspace.root), "--dataset", str(dataset), "--output", str(output)]
        )


def test_help_and_hybrid_authorization_before_services():
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0
    with pytest.raises(SystemExit):
        chapter.parse_args(["--mode", "hybrid"])


def test_output_location_is_checked_before_possible_paid_service(components, monkeypatch):
    workspace, dataset, _, _, _ = components
    backend = Mock(side_effect=AssertionError("不得启动服务"))
    monkeypatch.setattr(chapter, "ZoektBackend", backend)
    with pytest.raises(ValueError, match="之外"):
        chapter.main(
            [
                "--workspace",
                str(workspace.root),
                "--dataset",
                str(dataset),
                "--mode",
                "hybrid",
                "--allow-remote-embedding",
                "--output",
                str(workspace.root / "bad.json"),
            ]
        )
    backend.assert_not_called()


def test_partial_results_remain_partial_and_still_are_scored(components):
    workspace, dataset, _, retriever, _ = components
    original = retriever.search

    def partial(query, **options):
        result = original(query, **options)
        data = json.loads(result.content)
        data["status"] = "partial"
        return ToolResult(json.dumps(data), result.details)

    retriever.search = partial
    locator = ComponentLocator(workspace=workspace, mode="text", retriever=retriever)
    report = EvaluationRunner(workspace=workspace, locator=locator).run(
        load_cases(dataset, workspace=workspace)
    )
    assert report["cases"][0]["status"] == "partial"
    assert report["cases"][0]["file_recall_at_10"] == 1


def test_corrupt_retrieval_object_counts_failure_not_crash(components):
    workspace, dataset, _, _, _ = components
    fake = SimpleNamespace(search=lambda *args, **kwargs: ToolResult("[]"))
    locator = ComponentLocator(workspace=workspace, mode="text", retriever=fake)
    report = EvaluationRunner(workspace=workspace, locator=locator).run(
        load_cases(dataset, workspace=workspace)
    )
    assert report["failed_cases"] == 1


def test_cost_parser_rejects_corruption_without_modifying_original(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type":"unknown"}\n', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="损坏"):
        read_session_stats(path)
    assert path.read_bytes() == before
