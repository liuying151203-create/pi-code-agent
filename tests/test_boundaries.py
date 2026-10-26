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


def test_early_chapters_preserve_the_pre_event_loop():
    paths = [
        ROOT / "tutorials" / chapter / "code.py"
        for chapter in ("s01_agent_loop", "s02_tools", "s03_models")
    ]
    loops = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loop = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "agent_loop"
        )
        loops.append(ast.dump(ast.Module(body=loop.body[1:], type_ignores=[])))
    assert len(loops) == 3
    assert len(set(loops)) == 1


def test_s04_preserves_the_complete_current_product_loop():
    paths = [ROOT / "src/pi_code_agent/loop.py", ROOT / "tutorials/s04_events/code.py"]
    loops = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loop = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "agent_loop"
        )
        loops.append(ast.dump(ast.Module(body=loop.body[1:], type_ignores=[])))
    assert loops[0] == loops[1]


def test_v1_archive_matches_every_original_file():
    manifest = json.loads((ROOT / "archive/v1-manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"].startswith("8a99dc7")
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / "archive/v1" / name).read_bytes()).hexdigest() == expected, (
            name
        )
