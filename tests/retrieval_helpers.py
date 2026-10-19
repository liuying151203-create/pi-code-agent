"""s18/s19 共用的离线替身；不代表真实 Zoekt 或 embedding 模型的检索效果。"""

import ast
import json
import shutil
from pathlib import Path

from s18_semantic_retrieval.embeddings import EmbeddingBatch

EXAMPLE = Path(__file__).resolve().parents[1] / "example_project" / "retrieval_demo"


def example_workspace(tmp_path, workspace_type):
    """复制受版本管理的示例，不携带运行缓存，不改原件。"""
    target = tmp_path / "repo"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".indexes", "__pycache__"))
    return workspace_type(target, tmp_path)


def payload(result):
    """将错误结果也解码成可断言的状态。"""
    return json.loads(result.content.removeprefix("Error: "))


class FakeEmbedding:
    """固定概念规则，仅用于验证缓存/排名/故障链路，不当作真实语义算法。"""

    cache_key = "offline-concepts-v1"

    def __init__(self):
        self.calls = []
        self.tokens = 7

    def embed(self, texts):
        self.calls.append(texts)
        vectors = [
            [1.0, 0.0]
            if any(word in text for word in ("负数", "normalize", "max(0"))
            else [0.0, 1.0]
            for text in texts
        ]
        return EmbeddingBatch(vectors, self.tokens)


class FakeZoekt:
    """只核验后端协议及位置，不验证真实索引或 ctags 覆盖。"""

    cache_key = "offline-zoekt-v1"

    def __init__(self):
        self.builds = 0
        self.modes = []

    def check_ready(self):
        pass

    def build_index(self, source_copy_dir, index_dir):
        self.builds += 1
        files = {
            path.relative_to(source_copy_dir).as_posix(): path.read_text(encoding="utf-8")
            for path in source_copy_dir.rglob("*.py")
        }
        (index_dir / "fake.zoekt").write_text(json.dumps(files), encoding="utf-8")

    def search_index(self, index_dir, query, scope, limit, mode="text"):
        self.modes.append(mode)
        files = json.loads((index_dir / "fake.zoekt").read_text(encoding="utf-8"))
        hits = []
        for path, source in sorted(files.items()):
            if scope and not path.startswith(scope + "/"):
                continue
            if mode == "symbol":
                lines = sorted(
                    {
                        node.lineno
                        for node in ast.walk(ast.parse(source))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and query in node.name
                    }
                )
            else:
                lines = [
                    number for number, line in enumerate(source.split("\n"), 1) if query in line
                ]
            if lines:
                hits.append({"path": path, "lines": lines[:3]})
        return {"files": hits[:limit], "exhaustive": True, "truncated": len(hits) > limit}
