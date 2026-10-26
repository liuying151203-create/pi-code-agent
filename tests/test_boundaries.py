"""新实现不依赖教学快照或归档，归档数据保持逐字节可核验。"""

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_imports_do_not_depend_on_tutorials_or_archive():
    for path in (ROOT / "src/pi_code_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            assert not any(name.startswith(("archive", "tutorials")) for name in imports)


def test_chapter_imports_are_local_and_cannot_follow_product_or_other_chapters():
    for chapter in (ROOT / "tutorials").glob("s*"):
        if not chapter.is_dir():
            continue
        for path in chapter.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    assert not any(
                        alias.name.startswith(("pi_code_agent", "archive", "tutorials"))
                        for alias in node.names
                    ), path
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        assert node.level == 1, path
                        assert (chapter / f"{node.module}.py").is_file(), (path, node.module)
                    else:
                        assert not (node.module or "").startswith(
                            ("pi_code_agent", "archive", "tutorials")
                        ), path


def test_chapter_sources_match_explicit_frozen_manifests():
    manifest = json.loads((ROOT / "tutorials/snapshots.json").read_text(encoding="utf-8"))
    for chapter, entry in manifest["chapters"].items():
        folder = ROOT / "tutorials" / chapter
        actual = {path.name for path in folder.glob("*.py")}
        assert actual == set(entry["files"]), chapter
        for name, expected in entry["files"].items():
            normalized = (folder / name).read_text(encoding="utf-8").encode("utf-8")
            assert hashlib.sha256(normalized).hexdigest() == expected, (
                chapter,
                name,
            )


def test_v1_archive_matches_every_original_file():
    manifest = json.loads((ROOT / "archive/v1-manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"].startswith("8a99dc7")
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / "archive/v1" / name).read_bytes()).hexdigest() == expected, (
            name
        )
