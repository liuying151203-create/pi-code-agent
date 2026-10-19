"""独立检索示例回归：只修改临时副本，普通测试不调用 Docker 或模型。"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from s16_repository_search import code as text_chapter
from s17_repository_symbols import code as symbol_chapter
from s17_repository_symbols.entities import EntityCatalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = PROJECT_ROOT / "example_project" / "retrieval_demo"
SOURCE_PATHS = {"math_ops/calc.py", "storage.py", "workflow.py"}


class ExampleBackend:
    """只验证示例的编排和结果协议，不冒充 Zoekt 的索引算法、排名或符号覆盖。"""

    cache_key = "offline-retrieval-example"

    def __init__(self):
        self.builds = 0

    def check_ready(self):
        """替身已就绪，不检查真实 Docker。"""

    def build_index(self, source_copy_dir, index_dir):
        """将过滤副本保存成测试专用数据，不产生真实 Zoekt 分片。"""
        self.builds += 1
        files = {
            path.relative_to(source_copy_dir).as_posix(): path.read_text(encoding="utf-8")
            for path in source_copy_dir.rglob("*.py")
        }
        (index_dir / "example.zoekt").write_text(json.dumps(files), encoding="utf-8")

    def search_index(self, index_dir, query, scope, limit, mode="text"):
        """用简单文本/AST 规则模拟该样本的返回位置，不验证真实引擎。"""
        files = json.loads((index_dir / "example.zoekt").read_text(encoding="utf-8"))
        hits = []
        truncated = False
        for path, source in sorted(files.items()):
            if scope and not path.startswith(scope + "/"):
                continue
            if mode == "symbol":
                lines = sorted(
                    {
                        node.lineno
                        for node in ast.walk(ast.parse(source))
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and query in node.name
                    }
                )
            else:
                lines = [
                    number for number, line in enumerate(source.splitlines(), 1) if query in line
                ]
            if lines:
                hits.append({"path": path, "lines": lines[:3]})
                truncated |= len(lines) > 3
        return {
            "files": hits[:limit],
            "exhaustive": True,
            "truncated": truncated or len(hits) > limit,
        }


@pytest.fixture
def example_copy(tmp_path):
    """所有可变测试使用临时副本，避免把索引更新试验写回示例原件。"""
    target = tmp_path / "harness" / "example_project" / "retrieval_demo"
    shutil.copytree(EXAMPLE_ROOT, target, ignore=shutil.ignore_patterns("__pycache__"))
    harness = tmp_path / "harness"
    original = {path: (EXAMPLE_ROOT / path).read_bytes() for path in SOURCE_PATHS}
    yield target, harness
    assert {path: (EXAMPLE_ROOT / path).read_bytes() for path in SOURCE_PATHS} == original


def payload(result):
    """解码工具正文，保留失败状态供断言使用。"""
    return json.loads(result.content.removeprefix("Error: "))


def test_example_business_runs_without_installation_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(EXAMPLE_ROOT / "workflow.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["checkout: 5", "preview: 9"]


@pytest.mark.parametrize("chapter", [text_chapter, symbol_chapter], ids=["s16", "s17"])
def test_default_workspace_is_one_example_not_harness_or_example_parent(
    chapter, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    options = chapter.parse_args([])
    assert options.workspace == EXAMPLE_ROOT
    assert options.workspace.is_dir()
    assert options.workspace != PROJECT_ROOT and options.workspace != EXAMPLE_ROOT.parent
    assert chapter.parse_args(["--workspace", str(tmp_path)]).workspace == tmp_path


def test_example_scan_and_entities_match_documented_positions(example_copy):
    target, harness = example_copy
    workspace = symbol_chapter.RepositoryWorkspace(target, harness)
    snapshot = workspace.scan()
    assert set(snapshot.file_contents) == SOURCE_PATHS
    assert "AGENTS.md" not in snapshot.file_contents and "README.md" not in snapshot.file_contents
    catalog = EntityCatalog.parse(workspace.workspace_id, snapshot)
    assert not catalog.parse_errors
    assert {
        (e.path, e.qualified_name, e.definition_line, e.start_line, e.end_line)
        for e in catalog.entities
    } == {
        ("math_ops/calc.py", "Calculator", 4, 4, 19),
        ("math_ops/calc.py", "Calculator.add", 8, 7, 19),
        ("math_ops/calc.py", "Calculator.add.normalize", 15, 15, 17),
        ("storage.py", "Store", 4, 4, 9),
        ("storage.py", "Store.add", 7, 7, 9),
        ("workflow.py", "checkout", 7, 7, 15),
        ("workflow.py", "preview", 18, 18, 25),
    }


def test_example_text_symbol_nested_and_alias_evidence(example_copy):
    target, harness = example_copy
    workspace = symbol_chapter.RepositoryWorkspace(target, harness)
    backend = ExampleBackend()
    index = symbol_chapter.RepositoryIndex(workspace=workspace, backend=backend)
    retriever = symbol_chapter.CodeRetriever(workspace=workspace, index_manager=index)
    text = payload(retriever.search("add"))
    assert text["status"] == "ok"
    text_files = {file["path"]: file for file in text["files"]}
    assert set(text_files) == SOURCE_PATHS
    assert [hit["start_line"] for hit in text_files["workflow.py"]["hits"]] == [13, 14, 24]
    assert [hit["entity"]["qualified_name"] for hit in text_files["workflow.py"]["hits"]] == [
        "checkout",
        "checkout",
        "preview",
    ]
    symbol = payload(retriever.search("add", mode="symbol"))
    assert {file["path"] for file in symbol["files"]} == {"math_ops/calc.py", "storage.py"}
    assert {
        hit["entity"]["qualified_name"] for file in symbol["files"] for hit in file["hits"]
    } == {
        "Calculator.add",
        "Store.add",
    }
    nested = payload(retriever.search("normalize", scope="math_ops"))
    assert [hit["entity"]["qualified_name"] for hit in nested["files"][0]["hits"]] == [
        "Calculator.add.normalize",
        "Calculator.add",
    ]
    entity = symbol["files"][0]["hits"][0]["entity"]
    read = workspace.dispatch(
        "read_file",
        {
            "path": entity["path"],
            "start_line": entity["start_line"],
            "limit": entity["end_line"] - entity["start_line"] + 1,
        },
    )
    assert "@staticmethod" in read and "def normalize" in read
    assert backend.builds == 1


def test_example_changes_create_new_generation_and_old_content_remains_reusable(example_copy):
    target, harness = example_copy
    workspace = symbol_chapter.RepositoryWorkspace(target, harness)
    backend = ExampleBackend()
    index = symbol_chapter.RepositoryIndex(workspace=workspace, backend=backend)
    retriever = symbol_chapter.CodeRetriever(workspace=workspace, index_manager=index)
    first = payload(retriever.search("add", mode="symbol"))
    same = payload(retriever.search("normalize", scope="math_ops"))
    assert first["index_generation"] == same["index_generation"] and backend.builds == 1
    calc = target / "math_ops/calc.py"
    original = calc.read_bytes()
    calc.write_bytes(original.replace(b"max(0, value)", b"max(1, value)"))
    changed = payload(retriever.search("add", mode="symbol"))
    assert changed["status"] == "ok" and backend.builds == 2
    assert changed["index_generation"] != first["index_generation"]
    old_dir = workspace.index_root / first["index_generation"]
    new_dir = workspace.index_root / changed["index_generation"]
    assert (old_dir / "source/math_ops/calc.py").read_bytes() == original
    assert b"max(1, value)" in (new_dir / "source/math_ops/calc.py").read_bytes()
    assert (old_dir / "entities.sqlite").is_file() and (new_dir / "entities.sqlite").is_file()
    calc.write_bytes(original)
    restored = payload(retriever.search("add", mode="symbol"))
    assert restored["index_generation"] == first["index_generation"] and backend.builds == 2


@pytest.mark.parametrize("chapter", [text_chapter, symbol_chapter], ids=["s16", "s17"])
def test_direct_default_entry_uses_example_without_loading_model(
    chapter, example_copy, monkeypatch, capsys
):
    target, harness = example_copy
    monkeypatch.setattr(chapter, "HARNESS_ROOT", harness)
    monkeypatch.setattr(chapter, "ZoektBackend", lambda _: ExampleBackend())

    def forbid_model(*_args, **_kwargs):
        pytest.fail("直接检索不应读取模型配置或创建 Provider")

    monkeypatch.setattr(chapter, "load_dotenv", forbid_model)
    monkeypatch.setattr(chapter, "create_model_provider", forbid_model)
    chapter.main(["--search", "add"])
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok" and {file["path"] for file in result["files"]} == SOURCE_PATHS
    assert not (harness / ".sessions").exists() and not (harness / ".traces").exists()
    assert target.is_dir()


@pytest.mark.skipif(
    os.getenv("ZERO2PI_TEST_ZOEKT") != "1", reason="需用户手动准备 s16 镜像并显式启用"
)
def test_real_zoekt_s16_example_queries(example_copy):
    target, harness = example_copy
    workspace = text_chapter.RepositoryWorkspace(target, harness)
    backend = text_chapter.ZoektBackend()
    backend.check_ready()
    index = text_chapter.RepositoryIndex(workspace=workspace, backend=backend)
    retriever = text_chapter.CodeRetriever(workspace=workspace, index_manager=index)
    result = payload(retriever.search("add"))
    assert result["status"] == "ok", result
    assert {file["path"] for file in result["files"]} == SOURCE_PATHS
    scoped = payload(retriever.search("normalize", scope="math_ops"))
    assert [file["path"] for file in scoped["files"]] == ["math_ops/calc.py"]


@pytest.mark.skipif(
    os.getenv("ZERO2PI_TEST_ZOEKT_S17") != "1", reason="需用户手动准备 s17 镜像并显式启用"
)
def test_real_zoekt_s17_example_queries(example_copy):
    target, harness = example_copy
    workspace = symbol_chapter.RepositoryWorkspace(target, harness)
    backend = symbol_chapter.ZoektBackend()
    backend.check_ready()
    index = symbol_chapter.RepositoryIndex(workspace=workspace, backend=backend)
    retriever = symbol_chapter.CodeRetriever(workspace=workspace, index_manager=index)
    text = payload(retriever.search("add"))
    symbol = payload(retriever.search("add", mode="symbol"))
    assert text["status"] == "ok" and symbol["status"] == "ok", (text, symbol)
    assert {file["path"] for file in text["files"]} == SOURCE_PATHS
    assert {file["path"] for file in symbol["files"]} == {"math_ops/calc.py", "storage.py"}
    assert {
        hit["entity"]["qualified_name"] for file in symbol["files"] for hit in file["hits"]
    } == {
        "Calculator.add",
        "Store.add",
    }
    assert text["index_generation"] == symbol["index_generation"]
