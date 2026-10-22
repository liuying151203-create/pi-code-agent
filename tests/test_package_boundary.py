"""正式源码不能借教程、兄弟产品或原仓库才能运行。"""

import ast
from pathlib import Path

import pi_code_agent


def test_production_imports_do_not_reach_tutorials_or_repository_product():
    root = Path(pi_code_agent.__file__).parent
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(
                    (
                        "tutorials",
                        "zero2pi",
                        "pi_repo_agent",
                        "s01_",
                        "s02_",
                        "s03_",
                        "s04_",
                        "s05_",
                        "s06_",
                        "s07_",
                        "s08_",
                        "s09_",
                        "s1",
                        "s2",
                    )
                ), (source, name)
