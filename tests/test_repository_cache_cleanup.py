"""s16/s17 索引清理回归：只操作临时项目，不运行 Docker 或清理真实缓存。"""

import os
from types import SimpleNamespace

import pytest

from s16_repository_search import cache_cleanup as cleanup
from s16_repository_search import code as text_chapter
from s17_repository_symbols import code as symbol_chapter


class Backend:
    """只生成固定的离线分片，用真实索引管理器发布测试代次。"""

    cache_key = "cleanup-test-image"

    def build_index(self, source_copy_dir, index_dir):
        (index_dir / "fake.zoekt").write_bytes(b"test-index")


@pytest.fixture(params=[text_chapter, symbol_chapter], ids=["s16", "s17"])
def components(request, tmp_path):
    target = tmp_path / "target"
    harness = tmp_path / "harness"
    target.mkdir()
    harness.mkdir()
    chapter = request.param
    workspace = chapter.RepositoryWorkspace(target, harness)
    index = chapter.RepositoryIndex(workspace=workspace, backend=Backend())
    source = target / "app.py"
    generations = []
    contents = []
    for version in range(4):
        content = f"def value():\n    return {version}\n".encode()
        contents.append(content)
        source.write_bytes(content)
        prepared = index.prepare(workspace.scan())
        directory = prepared.index_dir.parent
        # 显式固定先后顺序，不依赖测试机器的文件系统时间分辨率。
        timestamp = (version + 1) * 1_000_000_000
        os.utime(directory, ns=(timestamp, timestamp))
        generations.append(directory)
    # 当前源码回到旧代次，验证不能只保留“最新创建”的目录。
    source.write_bytes(contents[1])
    return SimpleNamespace(
        chapter=chapter,
        workspace=workspace,
        harness=harness,
        source=source,
        generations=generations,
    )


def test_plan_preserves_current_and_recent_without_deleting(components):
    plan = cleanup.plan_cache_cleanup(components.workspace)
    generations = components.generations
    assert {item.name for item in plan.retained} == {generations[1].name, generations[3].name}
    assert {item.name for item in plan.remove} == {generations[0].name, generations[2].name}
    assert all(item.size_bytes > 0 for item in plan.remove)
    assert not plan.skipped
    assert all(path.exists() for path in generations)
    keep_more = cleanup.plan_cache_cleanup(components.workspace, keep_recent=3)
    assert [item.name for item in keep_more.remove] == [generations[0].name]


def test_cleanup_only_removes_previewed_generations(components):
    workspace = components.workspace
    other_chapter = "s17" if workspace.index_root.name == "s16" else "s16"
    other_root = workspace.root / ".indexes" / other_chapter
    other_root.mkdir()
    sentinel = other_root / "do-not-delete.txt"
    sentinel.write_text("other chapter", encoding="utf-8")
    original_source = components.source.read_bytes()
    plan = cleanup.plan_cache_cleanup(workspace)
    assert cleanup.apply_cache_cleanup(workspace, plan) == 2
    assert components.source.read_bytes() == original_source
    assert sentinel.read_text(encoding="utf-8") == "other chapter"
    assert [path.exists() for path in components.generations] == [False, True, False, True]
    assert not (components.harness / ".sessions").exists()
    assert not (components.harness / ".traces").exists()


@pytest.mark.parametrize("change", ["source", "cache"])
def test_change_after_preview_stops_before_any_deletion(components, change):
    plan = cleanup.plan_cache_cleanup(components.workspace)
    if change == "source":
        components.source.write_text("changed\n", encoding="utf-8")
    else:
        (components.generations[0] / "notes.txt").write_text("unknown", encoding="utf-8")
    with pytest.raises(RuntimeError, match="重新预览"):
        cleanup.apply_cache_cleanup(components.workspace, plan)
    assert all(path.exists() for path in components.generations)


@pytest.mark.parametrize(
    "unsafe",
    ["unknown_file", "hardlink", "manifest", "manifest_list", "manifest_budget", "staging"],
)
def test_unknown_or_unsafe_cache_is_retained(components, unsafe, monkeypatch):
    generation = components.generations[0]
    if unsafe == "unknown_file":
        (generation / "notes.txt").write_text("keep", encoding="utf-8")
    elif unsafe == "hardlink":
        os.link(components.source, generation / "source" / "alias.py")
    elif unsafe == "manifest":
        (generation / "manifest.json").write_text("{}", encoding="utf-8")
    elif unsafe == "manifest_list":
        (generation / "manifest.json").write_text("[]", encoding="utf-8")
    elif unsafe == "manifest_budget":
        # 枚举仍看到正常大小，仅在实际读取时缩小预算，验证读取自身也有上限。
        original_read = cleanup._read_manifest

        def limited_read(path):
            with monkeypatch.context() as scoped_patch:
                scoped_patch.setattr(cleanup, "MAX_MANIFEST_BYTES", 1)
                return original_read(path)

        monkeypatch.setattr(cleanup, "_read_manifest", limited_read)
    else:
        staging = components.workspace.index_root / ".build-in-progress"
        staging.mkdir()
        (staging / "partial").write_bytes(b"keep")
    plan = cleanup.plan_cache_cleanup(components.workspace)
    assert plan.skipped
    if unsafe != "staging":
        assert generation.name not in {item.name for item in plan.remove}
    cleanup.apply_cache_cleanup(components.workspace, plan)
    if unsafe == "staging":
        assert (staging / "partial").read_bytes() == b"keep"
    else:
        assert generation.exists()


def test_wrong_workspace_and_too_many_entries_cannot_delete(components, tmp_path, monkeypatch):
    plan = cleanup.plan_cache_cleanup(components.workspace)
    other_target = tmp_path / "other"
    other_target.mkdir()
    other_workspace = components.chapter.RepositoryWorkspace(other_target, components.harness)
    with pytest.raises(ValueError, match="不属于当前工作区"):
        cleanup.apply_cache_cleanup(other_workspace, plan)
    monkeypatch.setattr(cleanup, "MAX_GENERATIONS", 1)
    with pytest.raises(ValueError, match="目录项"):
        cleanup.plan_cache_cleanup(components.workspace)
    assert all(path.exists() for path in components.generations)


@pytest.mark.parametrize("answer", ["", "n", "yes", "interrupt", "eof"])
def test_cancel_or_interrupt_does_not_delete(components, answer, monkeypatch, capsys):
    def respond(prompt):
        if answer == "interrupt":
            raise KeyboardInterrupt
        if answer == "eof":
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", respond)
    cleanup.run_cache_cleanup(components.workspace)
    assert "已取消" in capsys.readouterr().out
    assert all(path.exists() for path in components.generations)


def test_empty_cache_does_not_create_directories_or_ask_for_confirmation(
    components, tmp_path, monkeypatch, capsys
):
    empty_target = tmp_path / "empty-target"
    empty_target.mkdir()
    workspace = components.chapter.RepositoryWorkspace(empty_target, components.harness)

    def forbid_input(prompt):
        pytest.fail("没有候选时不应请求清理确认")

    monkeypatch.setattr("builtins.input", forbid_input)
    cleanup.run_cache_cleanup(workspace)
    assert "没有可清理" in capsys.readouterr().out
    assert not workspace.index_root.exists()


def test_partial_delete_failure_is_reported_without_touching_source(components, monkeypatch):
    plan = cleanup.plan_cache_cleanup(components.workspace)
    original_remove = cleanup.shutil.rmtree
    calls = []

    def fail_second(path):
        calls.append(path)
        if len(calls) == 2:
            raise PermissionError("测试文件占用")
        original_remove(path)

    before = components.source.read_bytes()
    monkeypatch.setattr(cleanup.shutil, "rmtree", fail_second)
    with pytest.raises(RuntimeError, match="已完整删除 1 个代次"):
        cleanup.apply_cache_cleanup(components.workspace, plan)
    assert components.source.read_bytes() == before
    assert all((plan.index_root / item.name).exists() for item in plan.retained)


def test_cli_confirmation_runs_without_backend_model_or_new_session(
    components, monkeypatch, capsys
):
    chapter = components.chapter
    monkeypatch.setattr(chapter, "HARNESS_ROOT", components.harness)

    def forbid(*args, **kwargs):
        pytest.fail("清理不应创建后端、模型或新索引")

    monkeypatch.setattr(chapter, "ZoektBackend", forbid)
    monkeypatch.setattr(chapter, "create_model_provider", forbid)
    monkeypatch.setattr(chapter, "load_dotenv", forbid)
    monkeypatch.setattr(components.workspace.__class__, "prepare_index_directory", forbid)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    chapter.main(["--workspace", str(components.workspace.root), "--clean-indexes"])
    assert "已删除 2 个旧索引代次" in capsys.readouterr().out
    assert [path.exists() for path in components.generations] == [False, True, False, True]
    assert not (components.harness / ".sessions").exists()


@pytest.mark.parametrize("chapter", [text_chapter, symbol_chapter], ids=["s16", "s17"])
@pytest.mark.parametrize(
    "arguments",
    [
        ["--keep-indexes", "2"],
        ["--clean-indexes", "--keep-indexes", "0"],
        ["--clean-indexes", "--search", "add"],
        ["--clean-indexes", "--session", "old.jsonl"],
        ["--clean-indexes", "--scope", "src"],
    ],
)
def test_cleanup_cli_rejects_mixed_or_invalid_modes(chapter, arguments):
    with pytest.raises(SystemExit) as stopped:
        chapter.parse_args(arguments)
    assert stopped.value.code == 2
