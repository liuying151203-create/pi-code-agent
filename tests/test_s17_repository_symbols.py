"""s17 离线回归：实体、同代发布、模式传递、只读补读和已有运行时组装。"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.session import SessionManager
from s16_repository_search import code as s16
from s17_repository_symbols import code
from s17_repository_symbols import entities as entity_module
from s17_repository_symbols.entities import EntityCatalog
from s17_repository_symbols.extension import SEARCH_TOOL, RepositoryExtension
from s17_repository_symbols.index import RepositoryIndex
from s17_repository_symbols.retrieval import MAX_RESULT_BYTES, CodeRetriever
from s17_repository_symbols.workspace import RepositoryWorkspace
from s17_repository_symbols.zoekt import CTAGS_VERSION, DEFAULT_IMAGE, ZOEKT_COMMIT, ZoektBackend


class FakeZoekt:
    """只供离线编排测试的后端替身；不是生产搜索算法，也不证明 ctags 正确性。"""

    cache_key = "offline-symbol-image"

    def __init__(self):
        self.builds = 0
        self.calls = []
        self.fail_build = False
        self.result = None

    def check_ready(self):
        pass

    def build_index(self, source_copy_dir, index_dir):
        self.builds += 1
        if self.fail_build:
            raise RuntimeError("模拟索引失败")
        files = {
            path.relative_to(source_copy_dir).as_posix(): path.read_text(encoding="utf-8")
            for path in source_copy_dir.rglob("*")
            if path.is_file()
        }
        (index_dir / "fake.zoekt").write_text(json.dumps(files), encoding="utf-8")

    def search_index(self, index_dir, query, scope, limit, mode="text"):
        self.calls.append((query, scope, limit, mode))
        if self.result is not None:
            return self.result
        files = json.loads((index_dir / "fake.zoekt").read_text(encoding="utf-8"))
        hits = []
        for path, source in sorted(files.items()):
            if scope and not path.startswith(scope + "/"):
                continue
            lines = []
            for number, line in enumerate(source.splitlines(), 1):
                if query in line and (
                    mode == "text" or line.lstrip().startswith(("def ", "async def ", "class "))
                ):
                    lines.append(number)
            if lines:
                hits.append({"path": path, "lines": lines[:3]})
        return {"files": hits[:limit], "exhaustive": True, "truncated": len(hits) > limit}


@pytest.fixture
def components(tmp_path):
    target = tmp_path / "target"
    harness = tmp_path / "harness"
    target.mkdir()
    harness.mkdir()
    (target / "app.py").write_text(
        "class Store:\n"
        "    @decorator\n"
        "    async def restore_state(self):\n"
        "        return 'restored'\n"
        "\n"
        "def caller():\n"
        "    return Store().restore_state()\n",
        encoding="utf-8",
    )
    workspace = RepositoryWorkspace(target, harness)
    backend = FakeZoekt()
    index = RepositoryIndex(workspace=workspace, backend=backend)
    retriever = CodeRetriever(workspace=workspace, index_manager=index)
    return SimpleNamespace(
        target=target,
        harness=harness,
        workspace=workspace,
        backend=backend,
        index=index,
        retriever=retriever,
    )


def payload(result):
    return json.loads(result.content.removeprefix("Error: "))


def test_parse_nested_async_decorated_and_duplicate_definitions(components):
    path = components.target / "app.py"
    path.write_text(
        "class Outer:\n"
        "    @decorate(\n"
        "        'x'\n"
        "    )\n"
        "    async def run(self):\n"
        "        def nested():\n"
        "            return 1\n"
        "        return nested()\n"
        "def run():\n"
        "    pass\n"
        "def run():\n"
        "    pass\n",
        encoding="utf-8",
    )
    catalog = EntityCatalog.parse(components.workspace.workspace_id, components.workspace.scan())
    assert [
        (e.qualified_name, e.kind, e.start_line, e.definition_line, e.end_line)
        for e in catalog.entities
    ] == [
        ("Outer", "class", 1, 1, 8),
        ("Outer.run", "async_function", 2, 5, 8),
        ("Outer.run.nested", "function", 6, 6, 7),
        ("run", "function", 9, 9, 10),
        ("run", "function", 11, 11, 12),
    ]
    assert len({e.entity_id for e in catalog.entities}) == 5
    assert catalog.entity_at_line("app.py", 7).qualified_name == "Outer.run.nested"
    assert catalog.entity_at_line("app.py", 2).qualified_name == "Outer.run"
    assert catalog.entity_at_line("app.py", 8, symbol_query="nested") is None
    assert catalog.entity_at_line("app.py", 5, symbol_query="run").qualified_name == "Outer.run"


def test_parser_never_imports_target_and_supports_bom_pyi_and_crlf(components):
    (components.target / "app.py").write_bytes(
        b"\xef\xbb\xbfraise RuntimeError('must not run')\r\ndef add():\r\n    return 1\r\n"
    )
    (components.target / "types.pyi").write_text("def typed() -> int: ...\n", encoding="utf-8")
    catalog = EntityCatalog.parse(components.workspace.workspace_id, components.workspace.scan())
    assert {(e.path, e.name, e.start_line, e.end_line) for e in catalog.entities} == {
        ("app.py", "add", 2, 3),
        ("types.pyi", "typed", 1, 1),
    }
    assert not catalog.parse_errors


def test_parse_error_does_not_remove_text_file(components):
    (components.target / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = payload(components.retriever.search("broken"))
    assert result["status"] == "ok"
    assert result["python_parse_error_count"] == 1
    assert "broken.py" in result["python_parse_errors"]
    assert result["files"][0]["hits"][0]["entity"] is None
    assert "broken.py" in components.workspace.scan().file_contents


def test_text_and_symbol_share_generation_but_not_query_meaning(components):
    text = payload(components.retriever.search("restore_state"))
    symbol = payload(components.retriever.search("restore_state", mode="symbol"))
    assert components.backend.builds == 1
    assert text["index_generation"] == symbol["index_generation"]
    assert [hit["start_line"] for hit in text["files"][0]["hits"]] == [3, 7]
    assert [hit["start_line"] for hit in symbol["files"][0]["hits"]] == [3]
    entity = symbol["files"][0]["hits"][0]["entity"]
    assert entity["qualified_name"] == "Store.restore_state"
    assert (entity["start_line"], entity["definition_line"], entity["end_line"]) == (2, 3, 4)
    assert text["files"][0]["hits"][1]["entity"]["qualified_name"] == "caller"
    assert components.backend.calls[-1] == ("restore_state", "", 10, "symbol")


def test_cache_contains_same_source_shards_and_sqlite_and_reuses_without_parse(
    components, monkeypatch
):
    snapshot = components.workspace.scan()
    prepared = components.index.prepare(snapshot)
    generation_dir = prepared.index_dir.parent
    assert {p.name for p in generation_dir.iterdir()} == {
        "source",
        "shards",
        "entities.sqlite",
        "manifest.json",
    }
    assert (generation_dir / "source/app.py").read_bytes() == snapshot.file_contents["app.py"]
    restored = EntityCatalog.load(
        generation_dir / "entities.sqlite", components.workspace.workspace_id, snapshot
    )
    assert restored.entities == prepared.entity_catalog.entities
    monkeypatch.setattr(EntityCatalog, "parse", lambda *_: pytest.fail("未变化不重新解析"))
    again = components.index.prepare(snapshot)
    assert not again.rebuilt and again.generation == prepared.generation
    assert again.entity_catalog.entities == prepared.entity_catalog.entities


def test_source_change_and_deletion_rebuild_entity_catalog(components):
    first = components.index.prepare(components.workspace.scan())
    (components.target / "app.py").write_text("def changed():\n    pass\n", encoding="utf-8")
    second = components.index.prepare(components.workspace.scan())
    assert first.generation != second.generation
    assert [e.name for e in second.entity_catalog.entities] == ["changed"]
    assert first.entity_catalog.entities[0].entity_id != second.entity_catalog.entities[0].entity_id
    (components.target / "app.py").unlink()
    third = components.index.prepare(components.workspace.scan())
    assert not third.entity_catalog.entities
    assert not list(third.index_dir.glob("*.zoekt"))
    assert components.backend.builds == 2


@pytest.mark.parametrize("stage", ["engine", "entities"])
def test_failed_build_never_publishes_partial_generation(components, monkeypatch, stage):
    if stage == "engine":
        components.backend.fail_build = True
    else:

        def fail_save(*_):
            raise RuntimeError("模拟数据库失败")

        monkeypatch.setattr(EntityCatalog, "save", fail_save)
    result = payload(components.retriever.search("restore_state", mode="symbol"))
    assert result["status"] == "failed" and not result["files"]
    assert not list(components.workspace.index_root.iterdir())
    assert components.workspace.search_hashes == {}


@pytest.mark.parametrize("corruption", ["missing", "garbage", "version", "range"])
def test_invalid_catalog_stops_instead_of_silent_text_fallback(components, corruption):
    prepared = components.index.prepare(components.workspace.scan())
    path = prepared.index_dir.parent / "entities.sqlite"
    if corruption == "missing":
        path.unlink()
    elif corruption == "garbage":
        path.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(path) as connection:
            if corruption == "version":
                connection.execute("UPDATE metadata SET value = 'wrong' WHERE key = 'source'")
            else:
                connection.execute("UPDATE entities SET end_line = 99999")
    result = payload(components.retriever.search("restore_state"))
    assert result["status"] == "failed" and not result["files"]
    assert not components.backend.calls


def test_source_changed_while_engine_queries_drops_entities_too(components, monkeypatch):
    original = components.backend.search_index

    def search_then_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        (components.target / "app.py").write_text("def new(): pass\n", encoding="utf-8")
        return result

    monkeypatch.setattr(components.backend, "search_index", search_then_edit)
    result = payload(components.retriever.search("restore_state", mode="symbol"))
    assert result["status"] == "stale" and not result["files"]
    assert components.workspace.search_hashes == {}


def test_read_entity_range_and_reject_changed_source_without_explicit_hash(components):
    result = payload(components.retriever.search("restore_state", mode="symbol"))
    entity = result["files"][0]["hits"][0]["entity"]
    read = components.workspace.dispatch(
        "read_file",
        {
            "path": "app.py",
            "start_line": entity["start_line"],
            "limit": entity["end_line"] - entity["start_line"] + 1,
        },
    )
    assert "@decorator" in read and "return 'restored'" in read and "def caller" not in read
    (components.target / "app.py").write_text("def changed(): pass\n", encoding="utf-8")
    read = components.workspace.dispatch("read_file", {"path": "app.py"})
    assert "版本已变化" in read


@pytest.mark.parametrize("mode", ["semantic", "SYMBOL", None, [], {}])
def test_invalid_mode_fails_before_scanning(components, monkeypatch, mode):
    monkeypatch.setattr(components.workspace, "scan", lambda: pytest.fail("不应扫描"))
    result = components.retriever.search("restore_state", mode=mode)
    assert payload(result)["status"] == "invalid"
    assert result.details["mode"] == "invalid"


def test_scope_and_file_limit_apply_to_both_modes(components):
    (components.target / "src").mkdir()
    (components.target / "src/other.py").write_text("def restore_state(): pass\n", encoding="utf-8")
    for mode in ("text", "symbol"):
        result = payload(
            components.retriever.search("restore_state", scope="src", limit=1, mode=mode)
        )
        assert [file["path"] for file in result["files"]] == ["src/other.py"]


def test_partial_and_budget_keep_original_ranking_and_metrics(components):
    components.backend.result = {
        "files": [{"path": "app.py", "lines": [3]}],
        "exhaustive": False,
        "truncated": True,
    }
    result = components.retriever.search("restore_state", mode="symbol")
    data = payload(result)
    assert data["status"] == "partial" and data["truncated"] and not data["search_exhaustive"]
    assert data["files"][0]["rank"] == 1
    assert components.workspace.search_hashes["app.py"] == data["files"][0]["content_hash"]
    assert result.details["mode"] == "symbol" and result.details["returned_files"] == 1
    assert len(result.content.encode()) <= MAX_RESULT_BYTES


def test_non_python_and_python_variable_symbol_have_no_fabricated_entity(components):
    (components.target / "note.txt").write_text("restore_state\n", encoding="utf-8")
    result = payload(components.retriever.search("restore_state"))
    assert result["files"][1]["hits"][0]["entity"] is None
    components.backend.result = {
        "files": [{"path": "app.py", "lines": [7]}],
        "exhaustive": True,
        "truncated": False,
    }
    # 替身可返回没有 AST 定义的符号；程序不拿 caller 的函数体边界伪装该符号定义。
    symbol = payload(components.retriever.search("restore_state", mode="symbol"))
    assert symbol["files"][0]["hits"][0]["entity"] is None


def test_runtime_records_are_chapter_and_target_isolated(components, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    workspace = components.workspace
    s16_workspace = s16.RepositoryWorkspace(components.target, components.harness)
    other_workspace = RepositoryWorkspace(other, components.harness)
    assert workspace.workspace_id == s16_workspace.workspace_id
    for attr in ("index_root", "session_root", "trace_root"):
        assert getattr(workspace, attr) != getattr(s16_workspace, attr)
        assert getattr(workspace, attr) != getattr(other_workspace, attr)
    assert not workspace.index_root.exists()
    assert not workspace.session_root.exists()
    assert not workspace.trace_root.exists()


@pytest.mark.parametrize("name", ["bash", "write_file", "edit_file"])
def test_write_and_shell_never_become_available(components, name):
    assert components.workspace.dispatch(name, {}).startswith("Permission denied:")
    assert "s17" in components.workspace.permission_hook(name, {})
    assert {t["name"] for t in components.workspace.tools} == {"read_file", "grep", "glob"}


def test_extension_registers_mode_and_writes_saved_metrics(components):
    session = SessionManager.open(components.workspace.select_session(None))
    api = ExtensionAPI(
        base_tools=components.workspace.tools,
        base_dispatch=components.workspace.dispatch,
        permission_hook=components.workspace.permission_hook,
        session=session,
    )
    metrics_path = components.workspace.trace_root / "test.retrieval.jsonl"
    extension = RepositoryExtension(retriever=components.retriever, metrics_path=metrics_path)
    extension.register(api)
    assert len(api.tools) == 4
    assert SEARCH_TOOL["input_schema"]["properties"]["mode"]["enum"] == ["text", "symbol"]
    result = api.dispatch("search_code", {"query": "restore_state", "mode": "symbol"})
    assert payload(result)["files"][0]["hits"][0]["entity"]["name"] == "restore_state"
    assert not metrics_path.exists()
    api.handle_event(
        code.AgentEvent(
            type="tool_result",
            data={
                "name": "search_code",
                "tool_call_id": "t1",
                "details": result.details,
            },
        )
    )
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["mode"] == "symbol"
    invalid = extension.handle_search({"query": "x", "regex": True})
    assert payload(invalid)["status"] == "invalid" and invalid.details["status"] == "invalid"


def test_direct_cli_passes_mode_without_model_or_session(components, monkeypatch, capsys):
    monkeypatch.setattr(code, "HARNESS_ROOT", components.harness)
    monkeypatch.setattr(code, "ZoektBackend", lambda _: components.backend)
    monkeypatch.setattr(code, "load_dotenv", lambda *_args, **_kwargs: pytest.fail("不读取配置"))
    code.main(
        [
            "--workspace",
            str(components.target),
            "--search",
            "restore_state",
            "--search-mode",
            "symbol",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "symbol"
    assert not (components.harness / ".sessions").exists()


def test_cli_help_and_validation_do_not_prepare_environment(monkeypatch, capsys):
    monkeypatch.setattr(code, "ZoektBackend", lambda *_: pytest.fail("help 不初始化后端"))
    with pytest.raises(SystemExit) as exit_info:
        code.main(["--help"])
    assert exit_info.value.code == 0
    assert "--search-mode" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        code.parse_args(["--search-mode", "symbol"])


def test_agent_loop_stays_local_and_unchanged_from_s16():
    assert code.agent_loop.__module__ == "s17_repository_symbols.code"
    assert ast.dump(ast.parse(inspect.getsource(code.agent_loop))) == ast.dump(
        ast.parse(inspect.getsource(s16.agent_loop))
    )


def test_backend_requires_ctags_label_and_pins_actual_image(monkeypatch):
    metadata = [
        {
            "Os": "linux",
            "Id": "sha256:known",
            "Config": {
                "Labels": {
                    "zero2pi.zoekt.commit": ZOEKT_COMMIT,
                    "zero2pi.zoekt.adapter": "s17-v1",
                    "zero2pi.ctags.version": CTAGS_VERSION,
                }
            },
        }
    ]
    calls = []

    def inspect_image(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(metadata))

    monkeypatch.setattr("s17_repository_symbols.zoekt.subprocess.run", inspect_image)
    backend = ZoektBackend()
    backend.check_ready()
    assert backend.cache_key == "sha256:known"
    assert calls == [["docker", "image", "inspect", DEFAULT_IMAGE]]
    del metadata[0]["Config"]["Labels"]["zero2pi.ctags.version"]
    with pytest.raises(RuntimeError, match="固定版本"):
        ZoektBackend().check_ready()


def test_backend_enables_required_ctags_and_passes_symbol_mode(components, monkeypatch):
    backend = ZoektBackend()
    calls = []

    def run(entrypoint, arguments, mounts, **kwargs):
        calls.append((entrypoint, arguments, mounts, kwargs))
        return json.dumps({"files": [], "exhaustive": True, "truncated": False})

    monkeypatch.setattr(backend, "_run", run)
    backend.build_index(components.target, components.harness)
    assert "-require_ctags" in calls[0][1] and "-disable_ctags" not in calls[0][1]
    backend.search_index(components.harness, "restore_state", "", 10, mode="symbol")
    assert json.loads(calls[-1][3]["input_text"])["mode"] == "symbol"
    with pytest.raises(ValueError, match="mode"):
        backend.search_index(components.harness, "x", "", 10, mode="semantic")


def test_entity_limit_stops_publication_instead_of_silently_skipping(components, monkeypatch):
    monkeypatch.setattr(entity_module, "MAX_ENTITIES", 1)
    result = payload(components.retriever.search("restore_state", mode="symbol"))
    assert result["status"] == "failed" and "实体超过" in result["error"]
    assert not list(components.workspace.index_root.iterdir())


def test_catalog_size_limit_is_checked_before_publication(components, monkeypatch):
    monkeypatch.setattr(entity_module, "MAX_CATALOG_BYTES", 1)
    result = payload(components.retriever.search("restore_state"))
    assert result["status"] == "failed" and "数据库超过" in result["error"]
    assert not list(components.workspace.index_root.iterdir())


def test_cached_entity_id_cannot_be_replaced_with_another_version(components):
    prepared = components.index.prepare(components.workspace.scan())
    with sqlite3.connect(prepared.index_dir.parent / "entities.sqlite") as connection:
        connection.execute("UPDATE entities SET entity_id = 'wrong' WHERE name = 'Store'")
    result = payload(components.retriever.search("restore_state"))
    assert result["status"] == "failed" and "实体身份" in result["error"]


def test_catalog_load_counts_each_source_file_once(components, monkeypatch):
    snapshot = components.workspace.scan()
    prepared = components.index.prepare(snapshot)
    calls = []
    original = entity_module.source_lines

    def count_lines(data):
        calls.append(data)
        return original(data)

    monkeypatch.setattr(entity_module, "source_lines", count_lines)
    loaded = EntityCatalog.load(
        prepared.index_dir.parent / "entities.sqlite", components.workspace.workspace_id, snapshot
    )
    assert len(loaded.entities) == 3
    assert len(calls) == 1


def test_parse_error_display_is_bounded_and_marked(components):
    for number in range(8):
        (components.target / f"broken{number}.py").write_text("def broken(:\n", encoding="utf-8")
    result = payload(components.retriever.search("restore_state", mode="symbol"))
    assert result["python_parse_error_count"] == 8
    assert len(result["python_parse_errors"]) == 5 and result["truncated"]
    assert result["status"] == "ok"


def test_large_entity_metadata_cannot_exceed_result_budget(components):
    name = "restore_" + "x" * 18000
    (components.target / "app.py").write_text(f"def {name}(): pass\n", encoding="utf-8")
    result = components.retriever.search("restore_", mode="symbol")
    assert len(result.content.encode()) <= MAX_RESULT_BYTES
    assert payload(result)["truncated"]
    assert not payload(result)["files"]
    assert not components.workspace.search_hashes


def test_protected_sources_never_enter_entity_catalog_or_copy(components):
    (components.target / ".env").write_text("SECRET=restore_state\n", encoding="utf-8")
    (components.target / ".git").mkdir()
    (components.target / ".git/secret.py").write_text("def secret(): pass\n", encoding="utf-8")
    prepared = components.index.prepare(components.workspace.scan())
    assert {e.path for e in prepared.entity_catalog.entities} == {"app.py"}
    assert not (prepared.index_dir.parent / "source/.env").exists()
    assert not (prepared.index_dir.parent / "source/.git").exists()
    assert components.workspace.dispatch("read_file", {"path": ".env"}).startswith(
        "Permission denied:"
    )


def test_backend_short_container_keeps_no_network_no_pull_and_original_repo_unmounted(
    components, monkeypatch
):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout='{"files":[],"exhaustive":true,"truncated":false}', stderr=""
        )

    monkeypatch.setattr("s17_repository_symbols.zoekt.subprocess.run", run)
    backend = ZoektBackend()
    backend.cache_key = "sha256:known"
    backend.search_index(components.harness, "restore_state", "", 10, mode="symbol")
    command = calls[0]
    assert "--network=none" in command and "--pull=never" in command and "--read-only" in command
    assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges" in command
    assert not any(str(components.target) in arg or "docker.sock" in arg for arg in command)
    assert "sha256:known" in command
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 1 and mounts[0].endswith(",readonly")


def test_agent_entry_searches_symbol_then_reads_entity_and_saves_metrics(
    components, monkeypatch, capsys
):
    from s10_model_provider.code import ModelResponse

    requests = []

    class Provider:
        model = "offline-s17"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                block = {
                    "type": "tool_use",
                    "id": "search-1",
                    "name": "search_code",
                    "input": {"query": "restore_state", "mode": "symbol"},
                }
            elif len(requests) == 2:
                found = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                entity = found["files"][0]["hits"][0]["entity"]
                block = {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {
                        "path": entity["path"],
                        "start_line": entity["start_line"],
                        "limit": entity["end_line"] - entity["start_line"] + 1,
                    },
                }
            else:
                assert "@decorator" in kwargs["messages"][-1]["content"][0]["content"]
                block = {"type": "text", "text": "Store.restore_state 在 app.py:2–4。"}
            return ModelResponse(
                model=self.model,
                usage=None,
                stop_reason="tool_use" if block["type"] == "tool_use" else "end_turn",
                content=[block],
            )

    monkeypatch.setattr(code, "HARNESS_ROOT", components.harness)
    monkeypatch.setattr(code, "ZoektBackend", lambda _: components.backend)
    monkeypatch.setattr(code, "create_model_provider", lambda **_: Provider())
    monkeypatch.setattr(code, "load_dotenv", lambda *_, **__: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    answers = iter(["查 restore_state 定义并补读", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    code.main(["--workspace", str(components.target)])
    assert len(requests) == 3 and all(len(request["tools"]) == 4 for request in requests)
    session_files = list(components.workspace.session_root.glob("*.jsonl"))
    assert len(session_files) == 1
    assert "Store.restore_state" in session_files[0].read_text(encoding="utf-8")
    metrics_path = next(components.workspace.trace_root.glob("*.retrieval.jsonl"))
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["mode"] == "symbol"
    assert "read-only" in capsys.readouterr().out


@pytest.mark.skipif(
    os.getenv("ZERO2PI_TEST_ZOEKT_S17") != "1", reason="需用户手动准备 s17 镜像并显式启用"
)
def test_real_zoekt_symbol_vs_text_and_updated_entities(components):
    backend = ZoektBackend()
    backend.check_ready()
    index = RepositoryIndex(workspace=components.workspace, backend=backend)
    retriever = CodeRetriever(workspace=components.workspace, index_manager=index)
    symbol = payload(retriever.search("restore_state", mode="symbol"))
    text = payload(retriever.search("restore_state", mode="text"))
    assert symbol["status"] == "ok", symbol
    assert [h["start_line"] for h in symbol["files"][0]["hits"]] == [3]
    assert [h["start_line"] for h in text["files"][0]["hits"]] == [3, 7]
    assert symbol["files"][0]["hits"][0]["entity"]["qualified_name"] == "Store.restore_state"
    old_generation = symbol["index_generation"]
    (components.target / "app.py").write_text("def other_name(): pass\n", encoding="utf-8")
    assert payload(retriever.search("restore_state", mode="symbol"))["status"] == "empty"
    changed = payload(retriever.search("other_name", mode="symbol"))
    assert changed["index_generation"] != old_generation
    entity = changed["files"][0]["hits"][0]["entity"]
    assert entity["name"] == "other_name"
    assert (
        entity["content_hash"]
        == hashlib.sha256((components.target / "app.py").read_bytes()).hexdigest()
    )
