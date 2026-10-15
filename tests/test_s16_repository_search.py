"""s16 回归：工作区、索引版本、检索证据与扩展闭环；普通测试不运行 Docker/模型。"""

import hashlib
import inspect
import json
import os
import subprocess
from dataclasses import fields
from types import SimpleNamespace

import pytest

from s11_runtime_observability.code import AgentEvent, ModelResponse
from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.session import SessionManager
from s16_repository_search import code as chapter
from s16_repository_search.extension import RepositoryExtension
from s16_repository_search.index import PreparedIndex, RepositoryIndex
from s16_repository_search.retrieval import MAX_RESULT_BYTES, CodeRetriever
from s16_repository_search.workspace import RepositoryWorkspace, source_lines
from s16_repository_search.zoekt import DEFAULT_IMAGE, ZOEKT_COMMIT, ZoektBackend


class FakeZoekt:
    """只验证后端协议及组装；不冒充真实 trigram 算法或 Zoekt 排名。"""

    cache_key = "offline-test-image"

    def __init__(self):
        self.builds = 0
        self.searches = 0

    def check_ready(self):
        pass

    def build_index(self, source_copy_dir, index_dir):
        self.builds += 1
        files = {
            p.relative_to(source_copy_dir).as_posix(): p.read_text(encoding="utf-8")
            for p in source_copy_dir.rglob("*")
            if p.is_file()
        }
        (index_dir / "fake.zoekt").write_text(json.dumps(files), encoding="utf-8")

    def search_index(self, index_dir, query, scope, limit):
        self.searches += 1
        files = json.loads((index_dir / "fake.zoekt").read_text(encoding="utf-8"))
        matches = []
        truncated = False
        for path, content in sorted(files.items()):
            if scope and not path.startswith(scope + "/"):
                continue
            lines = [n for n, line in enumerate(source_lines(content.encode()), 1) if query in line]
            if lines:
                matches.append({"path": path, "lines": lines[:3]})
                truncated |= len(lines) > 3
        return {
            "files": matches[:limit],
            "exhaustive": True,
            "truncated": truncated or len(matches) > limit,
        }


def make_components(tmp_path, *, files=None, backend=None):
    target = tmp_path / "target"
    target.mkdir()
    harness = tmp_path / "harness"
    harness.mkdir()
    for relative, text in (files or {"app.py": "def add(a, b):\n    return a + b\n"}).items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    workspace = RepositoryWorkspace(target, harness)
    backend = backend or FakeZoekt()
    index_manager = RepositoryIndex(workspace=workspace, backend=backend)
    retriever = CodeRetriever(workspace=workspace, index_manager=index_manager)
    return workspace, backend, index_manager, retriever


def payload(result):
    return json.loads(result.content.removeprefix("Error: "))


def test_workspace_has_no_constructor_side_effects_and_never_changes_old_globals(tmp_path):
    from s03_permission import code as old

    original = old.WORKDIR
    workspace, _, _, _ = make_components(tmp_path)
    assert old.WORKDIR == original
    assert not workspace.index_root.exists()
    assert not workspace.session_root.exists()
    assert not workspace.trace_root.exists()
    assert {tool["name"] for tool in workspace.tools} == {"glob", "grep", "read_file"}
    assert "app.py" in workspace.dispatch("glob", {"pattern": "*.py"})
    assert "def add" in workspace.dispatch("read_file", {"path": "app.py"})
    assert "Permission denied" in workspace.dispatch("bash", {"command": "dir"})


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        ".aws/credentials",
        ".git/config",
        ".indexes/old.json",
        "node_modules/app.py",
        "private.pem",
    ],
)
def test_protected_files_never_enter_snapshot_or_base_reading(tmp_path, relative):
    workspace, _, _, _ = make_components(tmp_path)
    path = workspace.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("do-not-expose", encoding="utf-8")
    assert relative not in workspace.scan().file_contents
    assert "do-not-expose" not in workspace.dispatch("read_file", {"path": relative})
    assert "do-not-expose" not in workspace.glob("**/*")


def test_env_example_allowed_binary_and_invalid_utf8_reported(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    (workspace.root / ".env.example").write_text("MODEL_ID=", encoding="utf-8")
    (workspace.root / "binary.bin").write_bytes(b"abc\x00xyz")
    (workspace.root / "invalid.txt").write_bytes(b"\xff\xfe")
    snapshot = workspace.scan()
    assert ".env.example" in snapshot.file_contents
    assert snapshot.skipped_counts["binary"] == snapshot.skipped_counts["non_utf8"] == 1
    assert "二进制" in workspace.dispatch("read_file", {"path": "binary.bin"})


def test_gitignore_is_applied_before_reading_and_untracked_source_is_indexed(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    subprocess.run(["git", "init", "--quiet", str(workspace.root)], check=True, capture_output=True)
    (workspace.root / ".gitignore").write_text("hidden.py\nnested/*.tmp\n", encoding="utf-8")
    (workspace.root / "hidden.py").write_text("secret", encoding="utf-8")
    (workspace.root / "nested").mkdir()
    (workspace.root / "nested" / "a.tmp").write_text("secret", encoding="utf-8")
    snapshot = workspace.scan()
    assert "app.py" in snapshot.file_contents
    assert "hidden.py" not in snapshot.file_contents
    assert "nested/a.tmp" not in snapshot.file_contents
    assert snapshot.skipped_counts["gitignored"] == 2
    assert "secret" not in workspace.dispatch("grep", {"path": "hidden.py", "pattern": "secret"})


def test_hardlinks_and_symlinks_are_not_followed(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.link(outside, workspace.root / "alias.txt")
    assert "alias.txt" not in workspace.scan().file_contents
    assert "outside-secret" not in workspace.dispatch("read_file", {"path": "alias.txt"})
    try:
        (workspace.root / "link.txt").symlink_to(outside)
    except OSError:
        return  # Windows 可能未授予创建符号链接权限；硬链接验证仍实际执行。
    assert "link.txt" not in workspace.scan().file_contents
    assert "outside-secret" not in workspace.dispatch("read_file", {"path": "link.txt"})


@pytest.mark.parametrize("value", ["../outside.txt", "sub/../app.py", "", None])
def test_file_paths_cannot_escape_or_bypass_allowlist(tmp_path, value):
    workspace, _, _, _ = make_components(tmp_path)
    assert workspace.permission_hook("read_file", {"path": value}) is not None


# s16 回归 B023：目录是参数用错，不应误报权限不足或自动切换工具执行。
@pytest.mark.parametrize("tool_name", ["grep", "read_file"])
@pytest.mark.parametrize("directory", [".", "src"])
def test_directory_argument_reports_tool_guidance_not_permission_denied(
    tmp_path, tool_name, directory
):
    workspace, _, _, _ = make_components(tmp_path, files={"src/app.py": "needle\n"})
    session = SessionManager.open(workspace.select_session(None))
    api = make_api(workspace, session)
    arguments = {"path": directory}
    if tool_name == "grep":
        arguments["pattern"] = "needle"
    output = api.dispatch(tool_name, arguments)
    assert output.startswith("Error:")
    assert "单个文件" in output and "Permission denied" not in output
    assert ("search_code" if tool_name == "grep" else "glob") in output
    assert "1: needle" not in output


def test_directory_guidance_does_not_relax_outside_or_protected_paths(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    (workspace.root / ".aws").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for path in (str(outside), ".aws"):
        output = workspace.dispatch("grep", {"path": path, "pattern": "needle"})
        assert output.startswith("Permission denied:")


# s16 回归 B024：走真实注册层和工作区分发时，每次读取只枚举一次，下一次仍重新检查。
@pytest.mark.parametrize("tool_name", ["read_file", "grep"])
def test_registered_file_tool_checks_current_allowlist_once_per_call(
    tmp_path, monkeypatch, tool_name
):
    workspace, _, _, _ = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    api = make_api(workspace, session)
    original_eligible_paths = workspace._eligible_paths
    enumerations = []

    def eligible_paths():
        enumerations.append(True)
        return original_eligible_paths()

    monkeypatch.setattr(workspace, "_eligible_paths", eligible_paths)
    arguments = {"path": "app.py"}
    if tool_name == "grep":
        arguments["pattern"] = "add"
    assert "def add" in api.dispatch(tool_name, arguments)
    assert len(enumerations) == 1
    assert "def add" in api.dispatch(tool_name, arguments)
    assert len(enumerations) == 2


def test_gitignore_change_is_checked_again_before_next_read(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    subprocess.run(["git", "init", "--quiet", str(workspace.root)], check=True, capture_output=True)
    session = SessionManager.open(workspace.select_session(None))
    api = make_api(workspace, session)
    assert "def add" in api.dispatch("read_file", {"path": "app.py"})
    (workspace.root / ".gitignore").write_text("app.py\n", encoding="utf-8")
    output = api.dispatch("read_file", {"path": "app.py"})
    assert output.startswith("Permission denied:") and "def add" not in output


# s16 回归 B025：通过真实扩展先搜后读，模型省略指纹时也应拒绝变化后的源码。
def test_search_then_read_without_explicit_hash_rejects_changed_file(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    api = make_api(workspace, session)
    RepositoryExtension(
        retriever=retriever, metrics_path=workspace.trace_root / "metrics.jsonl"
    ).register(api)
    assert payload(api.dispatch("search_code", {"query": "add"}))["status"] == "ok"
    assert "def add" in api.dispatch("read_file", {"path": "app.py"})
    (workspace.root / "app.py").write_text("def add(): return 9\n", encoding="utf-8")
    output = api.dispatch("read_file", {"path": "app.py"})
    assert output.startswith("Error:") and "版本已变化" in output
    assert "return 9" not in output


def test_final_read_still_rejects_file_replaced_with_hardlink(tmp_path, monkeypatch):
    workspace, _, _, _ = make_components(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    original_eligible_paths = workspace._eligible_paths

    def replace_after_listing():
        paths, skipped = original_eligible_paths()
        (workspace.root / "app.py").unlink()
        os.link(outside, workspace.root / "app.py")
        return paths, skipped

    monkeypatch.setattr(workspace, "_eligible_paths", replace_after_listing)
    output = workspace.dispatch("read_file", {"path": "app.py"})
    assert output.startswith("Error:") and "outside-secret" not in output


def test_grep_definition_matches_s16_limit_without_mutating_s03(tmp_path):
    from s03_permission import code as old

    workspace, _, _, _ = make_components(tmp_path)
    grep_tool = next(tool for tool in workspace.tools if tool["name"] == "grep")
    old_grep = next(tool for tool in old.READ_ONLY_TOOLS if tool["name"] == "grep")
    assert "单个" in grep_tool["description"] and "search_code" in grep_tool["description"]
    assert grep_tool["input_schema"]["properties"]["limit"]["maximum"] == 50
    assert "maximum" not in old_grep["input_schema"]["properties"]["limit"]


# s16 新增：指纹只是搜索证据，不能扩大权限；未搜索文件不被强制要求先搜一次。
def test_unsearched_file_can_be_read_and_other_search_does_not_forget_old_evidence(tmp_path):
    workspace, _, _, retriever = make_components(
        tmp_path, files={"app.py": "add\n", "other.py": "needle\n"}
    )
    retriever.search("add")
    saved_hash = workspace.search_hashes["app.py"]
    assert "needle" in workspace.read_file("other.py")
    assert "other.py" not in workspace.search_hashes
    (workspace.root / "app.py").write_text("changed\n", encoding="utf-8")
    retriever.search("needle")
    assert workspace.search_hashes["app.py"] == saved_hash
    with pytest.raises(ValueError, match="版本已变化"):
        workspace.read_file("app.py")


def test_new_successful_search_refreshes_hash_and_new_workspace_does_not_restore_it(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    retriever.search("add")
    old_hash = workspace.search_hashes["app.py"]
    (workspace.root / "app.py").write_text("def add(): return 9\n", encoding="utf-8")
    retriever.search("add")
    assert workspace.search_hashes["app.py"] != old_hash
    assert "return 9" in workspace.read_file(str(workspace.root / "app.py"))
    restarted = RepositoryWorkspace(workspace.root, tmp_path / "harness")
    assert restarted.search_hashes == {}


def test_explicit_hash_keeps_precedence_and_empty_hash_is_not_silently_replaced(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    retriever.search("add")
    with pytest.raises(ValueError, match="版本已变化"):
        workspace.read_file("app.py", expected_hash="")
    (workspace.root / "app.py").write_text("new\n", encoding="utf-8")
    current_hash = hashlib.sha256((workspace.root / "app.py").read_bytes()).hexdigest()
    assert "1: new" in workspace.read_file("app.py", expected_hash=current_hash)
    with pytest.raises(ValueError, match="版本已变化"):
        workspace.read_file("app.py")


def test_verified_partial_results_also_register_hashes(tmp_path, monkeypatch):
    workspace, backend, _, retriever = make_components(tmp_path)
    original_search = backend.search_index

    def incomplete_search(*arguments):
        result = original_search(*arguments)
        result["exhaustive"] = False
        return result

    monkeypatch.setattr(backend, "search_index", incomplete_search)
    result = payload(retriever.search("add"))
    assert result["status"] == "partial"
    assert workspace.search_hashes["app.py"] == result["files"][0]["content_hash"]


def test_large_files_are_skipped_total_budget_fails_and_scan_errors_do_not_disappear(
    tmp_path, monkeypatch
):
    from s16_repository_search import workspace as module

    workspace, _, _, _ = make_components(tmp_path)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 100)
    (workspace.root / "large.py").write_bytes(b"x" * 101)
    assert workspace.scan().skipped_counts["large_file"] == 1
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 5)
    with pytest.raises(ValueError, match="源码超过"):
        workspace.scan()
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 10000)
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="目录项"):
        workspace.scan()


def test_read_window_has_lines_continuation_hash_and_stale_guard(tmp_path):
    workspace, _, _, _ = make_components(tmp_path, files={"app.py": "中文\nsecond\nthird\n"})
    source = workspace.scan()
    output = workspace.read_file(
        "app.py", start_line=2, limit=1, expected_hash=source.file_hashes["app.py"]
    )
    assert "2: second" in output and "start_line=3" in output
    assert "1: 中文" not in output
    (workspace.root / "app.py").write_text("new\n", encoding="utf-8")
    with pytest.raises(ValueError, match="版本已变化"):
        workspace.read_file("app.py", expected_hash=source.file_hashes["app.py"])
    with pytest.raises(ValueError):
        workspace.read_file("app.py", start_line=True)


def test_lf_line_numbers_match_zoekt_even_with_crlf_and_unicode_separators(tmp_path):
    workspace, _, _, retriever = make_components(
        tmp_path, files={"app.py": "first\u2028still-first\nneedle\n"}
    )
    (workspace.root / "app.py").write_bytes(b"first\r\nneedle\r\n")
    assert "2: needle" in workspace.read_file("app.py")
    (workspace.root / "app.py").write_bytes("first\u2028still-first\nneedle\n".encode())
    assert payload(retriever.search("needle"))["files"][0]["hits"][0]["start_line"] == 2
    assert "1: first\u2028still-first" in workspace.read_file("app.py")


def test_base_output_bytes_are_bounded_including_continuation(tmp_path):
    workspace, _, _, _ = make_components(
        tmp_path, files={"app.py": ("needle" + "😀" * 600 + "\n") * 50}
    )
    assert len(workspace.read_file("app.py").encode()) <= 50 * 1024
    assert len(workspace.grep("app.py", "needle").encode()) <= 50 * 1024


def test_scan_capacity_error_is_runtime_failure_not_invalid_query(tmp_path, monkeypatch):
    from s16_repository_search import workspace as module

    _, _, _, retriever = make_components(tmp_path)
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 1)
    assert payload(retriever.search("add"))["status"] == "failed"


# s16 新增：索引准备结果只保存位置和状态；命名整理不能改变磁盘格式与缓存身份。
def test_prepared_index_has_no_source_contents_and_preserves_cache_identity(tmp_path):
    workspace, backend, index_manager, _ = make_components(tmp_path)
    source_snapshot = workspace.scan()
    assert source_snapshot.file_contents["app.py"] == (workspace.root / "app.py").read_bytes()
    prepared_index = index_manager.prepare(source_snapshot)
    assert {field.name for field in fields(PreparedIndex)} == {"index_dir", "generation", "rebuilt"}
    expected_manifest = {
        "schema": "s16-v1",
        "workspace": workspace.workspace_id,
        "source": source_snapshot.fingerprint,
        "hashes": source_snapshot.file_hashes,
        "backend": backend.cache_key,
    }
    expected_generation = hashlib.sha256(
        json.dumps(expected_manifest, sort_keys=True).encode()
    ).hexdigest()
    assert prepared_index.generation == expected_generation
    assert prepared_index.index_dir == workspace.index_root / expected_generation / "shards"
    manifest_path = prepared_index.index_dir.parent / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == expected_manifest
    assert prepared_index.rebuilt is True
    assert index_manager.prepare(source_snapshot).rebuilt is False


# s16 新增：直接观察真实组装的调用顺序，保证建索引和核验使用同一份查询源码。
def test_retriever_flow_scans_prepares_queries_builds_results_then_verifies(tmp_path, monkeypatch):
    workspace, backend, index_manager, retriever = make_components(tmp_path)
    steps = []
    snapshots = []
    original_scan = workspace.scan
    original_prepare = index_manager.prepare
    original_search_index = backend.search_index
    original_build_file_results = retriever._build_file_results

    def scan():
        steps.append("scan" if not snapshots else "verify")
        snapshot = original_scan()
        snapshots.append(snapshot)
        return snapshot

    def prepare(source_snapshot):
        steps.append("prepare")
        assert source_snapshot is snapshots[0]
        return original_prepare(source_snapshot)

    def search_index(index_dir, query, scope, limit):
        steps.append("query")
        assert index_dir.parent.parent == workspace.index_root
        return original_search_index(index_dir, query, scope, limit)

    def build_file_results(file_matches, source_snapshot, query, scope, limit):
        steps.append("build_results")
        assert source_snapshot is snapshots[0]
        return original_build_file_results(file_matches, source_snapshot, query, scope, limit)

    monkeypatch.setattr(workspace, "scan", scan)
    monkeypatch.setattr(index_manager, "prepare", prepare)
    monkeypatch.setattr(backend, "search_index", search_index)
    monkeypatch.setattr(retriever, "_build_file_results", build_file_results)
    result = retriever.search("add")
    assert steps == ["scan", "prepare", "query", "build_results", "verify"]
    assert snapshots[0] is not snapshots[1]
    assert snapshots[0].fingerprint == snapshots[1].fingerprint
    assert payload(result)["status"] == "ok"
    assert payload(result)["files"][0]["content_hash"] == snapshots[0].file_hashes["app.py"]
    assert all(
        result.details[key] is not None for key in ("scan_ms", "index_ms", "search_ms", "verify_ms")
    )


def test_index_reuses_disk_cache_and_updates_changed_deleted_untracked_files(tmp_path):
    workspace, backend, index, retriever = make_components(tmp_path)
    first = payload(retriever.search("add"))
    again = payload(retriever.search("add"))
    assert first["index_generation"] == again["index_generation"]
    assert backend.builds == 1
    assert first["files"][0]["path"] == "app.py"
    restarted = RepositoryIndex(workspace=workspace, backend=backend)
    assert restarted.prepare(workspace.scan()).rebuilt is False
    (workspace.root / "app.py").write_text("def subtract(): pass\n", encoding="utf-8")
    changed = payload(retriever.search("add"))
    assert changed["status"] == "empty"
    assert changed["index_generation"] != first["index_generation"]
    (workspace.root / "new.py").write_text("def add(): pass\n", encoding="utf-8")
    assert payload(retriever.search("add"))["files"][0]["path"] == "new.py"
    (workspace.root / "new.py").unlink()
    assert payload(retriever.search("add"))["status"] == "empty"
    assert backend.builds == 3  # 删除后的内容与已有 subtract 快照相同，复用正确旧代次。
    backend.cache_key = "different-image"
    assert index.prepare(workspace.scan()).rebuilt is True


def test_failed_build_is_not_published_and_previous_generation_is_not_returned(
    tmp_path, monkeypatch
):
    workspace, backend, _, retriever = make_components(tmp_path)
    assert payload(retriever.search("add"))["status"] == "ok"
    saved_hashes = dict(workspace.search_hashes)
    (workspace.root / "app.py").write_text("changed", encoding="utf-8")

    def fail(source_dir, index_dir):
        (index_dir / "partial.zoekt").write_text("incomplete", encoding="utf-8")
        raise RuntimeError("测试构建失败")

    monkeypatch.setattr(backend, "build_index", fail)
    result = retriever.search("changed")
    assert result.content.startswith("Error:")
    assert payload(result)["status"] == "failed"
    assert payload(result)["files"] == []
    assert len(list(workspace.index_root.iterdir())) == 1
    assert workspace.search_hashes == saved_hashes


def test_corrupt_manifest_or_missing_shards_are_not_empty_results(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    result = payload(retriever.search("add"))
    generation = workspace.index_root / result["index_generation"]
    (generation / "shards" / "fake.zoekt").unlink()
    assert payload(retriever.search("add"))["status"] == "failed"
    (generation / "manifest.json").write_text("not-json", encoding="utf-8")
    assert payload(retriever.search("add"))["status"] == "failed"


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x\ny"},
        {"query": "x" * 257},
        {"query": 1},
        {"query": "add", "limit": True},
        {"query": "add", "limit": 21},
        {"query": "add", "scope": "../outside"},
        {"query": "add", "scope": "D:/repo"},
    ],
)
def test_invalid_search_does_not_scan_or_build(tmp_path, arguments):
    workspace, backend, _, retriever = make_components(tmp_path)
    assert payload(retriever.search(**arguments))["status"] == "invalid"
    assert backend.builds == backend.searches == 0
    assert not workspace.index_root.exists()


def test_query_is_literal_scope_is_applied_and_empty_is_distinct_from_partial(
    tmp_path, monkeypatch
):
    workspace, backend, _, retriever = make_components(
        tmp_path,
        files={"app.py": 'text = "file:secret OR .*"\n', "src/api.py": "needle\n"},
    )
    assert payload(retriever.search("file:secret OR .*"))["files"][0]["path"] == "app.py"
    assert payload(retriever.search("needle", scope="src"))["files"][0]["path"] == "src/api.py"
    assert payload(retriever.search("needle", scope="absent"))["status"] == "empty"
    monkeypatch.setattr(
        backend,
        "search_index",
        lambda *args: {"files": [], "exhaustive": False, "truncated": False},
    )
    result = payload(retriever.search("needle"))
    assert result["status"] == "partial" and result["search_exhaustive"] is False
    assert workspace.root.is_dir()


def test_returned_line_hash_and_snapshot_copy_match_original(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    result = payload(retriever.search("return"))
    file = result["files"][0]
    assert file["hits"][0]["start_line"] == 2
    assert (
        file["content_hash"] == hashlib.sha256((workspace.root / "app.py").read_bytes()).hexdigest()
    )
    copied = workspace.index_root / result["index_generation"] / "source" / "app.py"
    assert copied.read_bytes() == (workspace.root / "app.py").read_bytes()


def test_same_file_many_matches_does_not_occupy_multiple_file_ranks(tmp_path):
    _, _, _, retriever = make_components(
        tmp_path, files={"a.py": "needle\n" * 9, "b.py": "needle\n"}
    )
    result = payload(retriever.search("needle"))
    assert [file["rank"] for file in result["files"]] == [1, 2]
    assert len(result["files"][0]["hits"]) == 3 and result["truncated"] is True


def test_long_matching_line_keeps_evidence_and_json_remains_within_budget(tmp_path):
    files = {f"{n:02}.py": "a" * 2000 + "needle" + "中文" * 1000 + "\n" for n in range(20)}
    workspace, _, _, retriever = make_components(tmp_path, files=files)
    result = retriever.search("needle", limit=20)
    output = payload(result)
    assert len(result.content.encode()) <= MAX_RESULT_BYTES
    assert output["truncated"] is True
    assert len(output["files"]) < len(files)
    assert "needle" in output["files"][0]["hits"][0]["snippet"]
    assert output["files"][0]["hits"][0]["snippet_truncated"] is True
    # s16 新增：因正文预算被移除的文件不能被登记为模型已收到的搜索证据。
    assert workspace.search_hashes == {
        file["path"]: file["content_hash"] for file in output["files"]
    }


@pytest.mark.parametrize(
    "hit",
    [
        {"path": "../outside", "lines": [1]},
        {"path": "app.py", "lines": [999]},
        {"path": "app.py", "lines": [2]},
        {"path": "app.py", "lines": []},
    ],
)
def test_untrusted_backend_positions_are_not_evidence(tmp_path, monkeypatch, hit):
    _, backend, _, retriever = make_components(tmp_path)
    monkeypatch.setattr(
        backend,
        "search_index",
        lambda *args: {"files": [hit], "exhaustive": True, "truncated": False},
    )
    assert payload(retriever.search("add"))["status"] == "failed"


def test_source_change_during_search_returns_stale_not_old_evidence(tmp_path, monkeypatch):
    workspace, backend, _, retriever = make_components(tmp_path)
    original = backend.search_index

    def change(*arguments):
        result = original(*arguments)
        (workspace.root / "app.py").write_text("changed after query", encoding="utf-8")
        return result

    monkeypatch.setattr(backend, "search_index", change)
    result = payload(retriever.search("add"))
    assert result["status"] == "stale" and result["files"] == []
    assert workspace.search_hashes == {}


def test_newly_protected_or_unreadable_source_is_failure_not_complete_empty(tmp_path, monkeypatch):
    workspace, _, _, retriever = make_components(tmp_path)
    monkeypatch.setattr(workspace, "scan", lambda: (_ for _ in ()).throw(OSError("读取失败")))
    result = payload(retriever.search("add"))
    assert result["status"] == "failed" and result["search_exhaustive"] is False


def test_empty_workspace_skips_backend_query_but_publishes_explicit_empty_index(tmp_path):
    workspace, backend, _, retriever = make_components(tmp_path)
    (workspace.root / "app.py").unlink()
    assert payload(retriever.search("add"))["status"] == "empty"
    assert backend.builds == backend.searches == 0


def make_api(workspace, session):
    return ExtensionAPI(
        base_tools=workspace.tools,
        base_dispatch=workspace.dispatch,
        permission_hook=workspace.permission_hook,
        session=session,
    )


def test_extension_registers_real_components_and_records_compact_saved_metrics(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    metrics_path = workspace.trace_root / "retrieval.jsonl"
    extension = RepositoryExtension(retriever=retriever, metrics_path=metrics_path)
    api = make_api(workspace, session)
    extension.register(api)
    assert len(api.tools) == 4
    result = api.dispatch("search_code", {"query": "add"})
    assert result.details["returned_files"] == 1
    assert "query" not in result.details and "files" not in result.details
    event = AgentEvent(
        type="tool_result",
        data={"name": "search_code", "details": result.details, "tool_call_id": "call-1"},
    )
    api.handle_event(event)
    record = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert record["query_id"] == result.details["query_id"]
    assert record["tool_call_id"] == "call-1"
    assert "Permission denied" in api.dispatch("bash", {"command": "dir"})
    assert "Error" in api.dispatch("search_code", {"query": "x", "shell": "dir"}).content


def test_session_groups_are_isolated_and_resuming_other_target_is_refused(tmp_path):
    workspace, _, _, _ = make_components(tmp_path)
    path = workspace.select_session(None)
    SessionManager.open(path).append_message({"role": "user", "content": "你好"})
    assert workspace.select_session(path) == path
    other = tmp_path / "other"
    other.mkdir()
    second = RepositoryWorkspace(other, tmp_path / "harness")
    assert second.workspace_id != workspace.workspace_id
    with pytest.raises(ValueError, match="不属于"):
        second.select_session(path)


def test_full_loop_saves_details_before_metric_event_and_never_leaks_details_into_model(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    metrics_path = workspace.trace_root / "retrieval.jsonl"
    extension = RepositoryExtension(retriever=retriever, metrics_path=metrics_path)
    api = make_api(workspace, session)
    extension.register(api)
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return ModelResponse(
                model="offline",
                usage=None,
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "search-1",
                        "name": "search_code",
                        "input": {"query": "add"},
                    },
                ],
            )
        assert "details" not in kwargs["messages"][-1]["content"][0]
        return ModelResponse(
            model="offline",
            usage=None,
            stop_reason="end_turn",
            content=[{"type": "text", "text": "定位完成"}],
        )

    def emit(event):
        if event.type == "tool_result":
            assert session.latest_tool_details("search_code") is not None
        api.handle_event(event)

    chapter.agent_loop(
        [{"role": "user", "content": "定位 add"}],
        create_message=request,
        dispatch=api.dispatch,
        system=chapter.SYSTEM,
        hooks=api.hooks,
        emit=emit,
        tools=api.tools,
        save_assistant=session.append_assistant,
        save_tool_results=session.append_tool_results,
    )
    assert len(requests) == 2
    assert metrics_path.is_file()
    assert "def agent_loop(" in inspect.getsource(chapter)
    assert chapter.agent_loop is not chapter.previous.agent_loop


def test_help_and_offline_search_do_not_load_model_or_create_sessions(
    tmp_path, monkeypatch, capsys
):
    workspace, _, _, _ = make_components(tmp_path)
    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path / "harness")
    monkeypatch.setattr(chapter, "ZoektBackend", lambda image: FakeZoekt())
    monkeypatch.setattr(
        chapter, "create_model_provider", lambda **kwargs: pytest.fail("不得请求模型")
    )
    monkeypatch.setattr(
        chapter, "load_dotenv", lambda *args, **kwargs: pytest.fail("不读取模型配置")
    )
    with pytest.raises(SystemExit) as error:
        chapter.main(["--help"])
    assert error.value.code == 0
    capsys.readouterr()
    chapter.main(["--workspace", str(workspace.root), "--search", "add"])
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert not workspace.session_root.exists()
    assert not workspace.trace_root.exists()


def test_main_offline_provider_completes_search_then_reads_same_target(
    tmp_path, monkeypatch, capsys
):
    workspace, _, _, _ = make_components(tmp_path)
    requests = []

    class Provider:
        model = "offline-s16"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                block = {
                    "type": "tool_use",
                    "id": "search-1",
                    "name": "search_code",
                    "input": {"query": "add"},
                }
            elif len(requests) == 2:
                result = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                file = result["files"][0]
                block = {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {
                        "path": file["path"],
                        # s16 修改：模拟模型遗漏可选指纹，验证真实 main 仍能完成自动校验链路。
                        "limit": 2,
                    },
                }
            else:
                assert "1: def add" in kwargs["messages"][-1]["content"][0]["content"]
                block = {"type": "text", "text": "app.py 的 add 返回两数之和"}
            return ModelResponse(
                model=self.model,
                usage=None,
                stop_reason="tool_use" if block["type"] == "tool_use" else "end_turn",
                content=[block],
            )

    monkeypatch.setattr(chapter, "HARNESS_ROOT", tmp_path / "harness")
    monkeypatch.setattr(chapter, "ZoektBackend", lambda image: FakeZoekt())
    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    answers = iter(["定位 add 并解释", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    chapter.main(["--workspace", str(workspace.root)])
    assert len(requests) == 3
    assert [tool["name"] for tool in requests[0]["tools"]] == [
        "grep",
        "read_file",
        "glob",
        "search_code",
    ]
    assert str(workspace.root) in requests[0]["system"]
    assert len(list(workspace.session_root.glob("*.jsonl"))) == 1
    assert len(list(workspace.trace_root.glob("*.retrieval.jsonl"))) == 1
    assert "模式：read-only" in capsys.readouterr().out


def inspect_result(*, commit=ZOEKT_COMMIT, image_id="sha256:offline", os_name="linux"):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "Os": os_name,
                    "Id": image_id,
                    "Config": {
                        "Labels": {
                            "zero2pi.zoekt.commit": commit,
                            "zero2pi.zoekt.adapter": "s16-v1",
                        }
                    },
                }
            ]
        ),
        stderr="",
    )


def test_backend_checks_local_image_and_never_builds_or_pulls(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return inspect_result()

    monkeypatch.setattr(subprocess, "run", run)
    backend = ZoektBackend()
    backend.check_ready()
    assert backend.cache_key == "sha256:offline"
    assert calls == [["docker", "image", "inspect", DEFAULT_IMAGE]]


@pytest.mark.parametrize(
    "metadata",
    [
        inspect_result(commit="wrong"),
        inspect_result(os_name="windows"),
        SimpleNamespace(returncode=1, stdout="", stderr="missing"),
    ],
)
def test_backend_missing_or_wrong_image_stops_clearly(monkeypatch, metadata):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: metadata)
    backend = ZoektBackend()
    with pytest.raises(RuntimeError):
        backend.check_ready()
    assert backend.cache_key is None


def test_backend_mounts_only_filtered_copy_and_shards_without_shell_or_network(
    tmp_path, monkeypatch
):
    source = tmp_path / "filtered"
    index_dir = tmp_path / "shards"
    source.mkdir()
    index_dir.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout='{"files":[],"exhaustive":true,"truncated":false}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    backend = ZoektBackend()
    backend.cache_key = "sha256:offline"
    backend.build_index(source, index_dir)
    backend.search_index(index_dir, 'file:x OR .* "quote"', "src", 10)
    for command, kwargs in calls:
        assert "--pull=never" in command and "--network=none" in command
        assert "--read-only" in command and "--cap-drop=ALL" in command
        assert kwargs.get("shell", False) is False
        assert "sha256:offline" in command
    assert ",readonly" in calls[0][0][calls[0][0].index("--mount") + 1]
    assert json.loads(calls[1][1]["input"])["query"] == 'file:x OR .* "quote"'


def test_backend_timeout_removes_only_its_own_transient_container(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    backend = ZoektBackend()
    backend.cache_key = "sha256:offline"
    with pytest.raises(RuntimeError, match="超时"):
        backend.search_index(tmp_path, "needle", "", 10)
    container_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", container_name]


def test_component_recall_fixture_matches_basic_grep_evidence(tmp_path):
    """人工小样本只验证标签/文件去重口径，不声称真实模型或 Zoekt 性能提升。"""
    workspace, _, _, retriever = make_components(
        tmp_path,
        files={
            "api.py": "def retry_request():\n    cleanup()\n",
            "cleanup.py": "def cleanup(): pass\n",
            "test_api.py": "def test_retry_request(): pass\n",
        },
    )
    cases = [("retry_request", {"api.py", "test_api.py"}), ("cleanup", {"api.py", "cleanup.py"})]
    recalls = []
    for query, gold in cases:
        results = payload(retriever.search(query))["files"]
        ranked = [file["path"] for file in results]
        baseline = {
            path
            for path in workspace.scan().file_contents
            if workspace.grep(path, query) != "(no matches)"
        }
        assert set(ranked) == baseline
        recalls.append(len(set(ranked[:10]) & gold) / len(gold))
    assert sum(recalls) / len(recalls) == 1.0


@pytest.mark.skipif(os.getenv("ZERO2PI_TEST_ZOEKT") != "1", reason="需用户预先构建镜像并显式启用")
def test_real_zoekt_image_search_update_delete_and_literal_query(tmp_path):
    """真实引擎验证不下载/构建；用户准备镜像后按 README 单独执行。"""
    backend = ZoektBackend()
    backend.check_ready()
    workspace, _, _, retriever = make_components(
        tmp_path,
        backend=backend,
        files={
            "api.py": 'def retry_request():\n    return "中文 file:x OR .*"\n',
            "src/other.py": "retry_request()\n",
        },
    )
    result = payload(retriever.search("retry_request"))
    assert result["status"] == "ok"
    assert {file["path"] for file in result["files"]} == {"api.py", "src/other.py"}
    # s16 新增：真实引擎准备后，用基础 grep 校验同一范围的人工标签；不统计为 Agent 提升。
    gold = {"api.py", "src/other.py"}
    baseline = {
        path
        for path in workspace.scan().file_contents
        if workspace.grep(path, "retry_request") != "(no matches)"
    }
    assert baseline == gold
    assert len({file["path"] for file in result["files"][:10]} & gold) / len(gold) == 1.0
    assert payload(retriever.search("file:x OR .*"))["files"][0]["path"] == "api.py"
    assert (
        payload(retriever.search("retry_request", scope="src"))["files"][0]["path"]
        == "src/other.py"
    )
    (workspace.root / "api.py").unlink()
    assert {file["path"] for file in payload(retriever.search("retry_request"))["files"]} == {
        "src/other.py"
    }
    (workspace.root / "src/other.py").write_text("changed", encoding="utf-8")
    assert payload(retriever.search("retry_request"))["status"] == "empty"


def test_invalid_extension_arguments_are_recorded_as_failed_calls(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    metrics_path = workspace.trace_root / "retrieval.jsonl"
    extension = RepositoryExtension(retriever=retriever, metrics_path=metrics_path)
    api = make_api(workspace, session)
    extension.register(api)
    result = api.dispatch("search_code", {"query": "add", "extra": True})
    assert payload(result)["status"] == "invalid"
    api.handle_event(
        AgentEvent(
            type="tool_result",
            data={
                "name": "search_code",
                "tool_call_id": "invalid-1",
                "details": result.details,
            },
        )
    )
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["status"] == "invalid"


def test_metrics_observer_failure_stops_loop_after_saved_result(tmp_path, monkeypatch):
    workspace, _, _, retriever = make_components(tmp_path)
    session = SessionManager.open(workspace.select_session(None))
    extension = RepositoryExtension(
        retriever=retriever, metrics_path=workspace.trace_root / "metrics.jsonl"
    )
    api = make_api(workspace, session)

    def fail(event):
        raise OSError("测试指标写入失败")

    monkeypatch.setattr(extension, "record_metrics", fail)
    extension.register(api)
    events = chapter.EventDispatcher()
    events.subscribe(api.handle_event)
    requests = []

    def request(**kwargs):
        requests.append(kwargs)
        assert len(requests) == 1
        return ModelResponse(
            model="offline",
            usage=None,
            stop_reason="tool_use",
            content=[
                {
                    "type": "tool_use",
                    "id": "search-1",
                    "name": "search_code",
                    "input": {"query": "add"},
                },
            ],
        )

    with pytest.raises(chapter.EventDispatchError):
        chapter.agent_loop(
            [],
            create_message=request,
            dispatch=api.dispatch,
            system=chapter.SYSTEM,
            hooks=api.hooks,
            emit=events.emit,
            tools=api.tools,
            save_assistant=session.append_assistant,
            save_tool_results=session.append_tool_results,
        )
    assert len(requests) == 1
    assert session.latest_tool_details("search_code")["returned_files"] == 1
    assert session.build_context()[-1]["content"][0]["tool_use_id"] == "search-1"


def test_backend_cleanup_failure_does_not_claim_container_stopped(tmp_path, monkeypatch):
    def run(command, **kwargs):
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    backend = ZoektBackend()
    backend.cache_key = "sha256:offline"
    with pytest.raises(RuntimeError, match="未确认容器"):
        backend.search_index(tmp_path, "needle", "", 10)


@pytest.mark.parametrize(
    "output", ["not-json", "{}", '{"files":[],"exhaustive":1,"truncated":false}']
)
def test_backend_invalid_json_is_error_not_empty(tmp_path, monkeypatch, output):
    backend = ZoektBackend()
    backend.cache_key = "sha256:offline"
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: output)
    with pytest.raises(RuntimeError, match="有效 JSON"):
        backend.search_index(tmp_path, "needle", "", 10)


def test_cached_index_hardlink_cannot_expose_other_host_files(tmp_path):
    workspace, _, _, retriever = make_components(tmp_path)
    result = payload(retriever.search("add"))
    shards = workspace.index_root / result["index_generation"] / "shards"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.link(outside, shards / "unexpected.zoekt")
    failed = retriever.search("add")
    assert payload(failed)["status"] == "failed"
    assert "outside-secret" not in failed.content


def test_one_long_line_reports_display_truncation_without_incomplete_search(tmp_path):
    _, _, _, retriever = make_components(
        tmp_path, files={"app.py": "x" * 1000 + "needle" + "y" * 1000}
    )
    result = payload(retriever.search("needle"))
    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["search_exhaustive"] is True
