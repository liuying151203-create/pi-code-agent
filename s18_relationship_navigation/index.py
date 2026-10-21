"""s18：按源码版本保存关系元数据；不再保存一份源码副本。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from s16_repository_search.entities import EntityCatalog
from s16_repository_search.workspace import SourceSnapshot, checked_cache_path, source_lines
from s18_relationship_navigation.relations import MAX_RELATIONS, Relation, RelationshipGraph
from s18_relationship_navigation.workspace import RepositoryWorkspace

MAX_GRAPH_BYTES = 64 * 1024 * 1024


# s18 新增：独立关系代次与源码指纹绑定；语义/Zoekt 缓存仍由原组件管理。
class RelationshipIndex:
    """复用或构建当前源码对应的关系图。

    输入：工作区与 prepare 的源码快照；输出：RelationshipGraph 与是否重建。
    流程：计算版本身份 → 验证/加载 SQLite；不存在时解析 → 临时保存 → 原子发布。
    边界：不请求 embedding，不执行代码、不复制源码；损坏缓存明确报错，不冒充空图。
    """

    def __init__(self, *, workspace: RepositoryWorkspace) -> None:
        """仅保存目录依赖；没有提前建图或创建文件。"""
        self.workspace = workspace

    def prepare(
        self, snapshot: SourceSnapshot, *, catalog: EntityCatalog | None = None
    ) -> tuple[RelationshipGraph, bool]:
        """准备同版图；缓存读取同样核对节点、位置和版本。

        输入：已过滤的同次扫描；输出：图和 rebuilt 布尔值。
        流程：解析实体目录 → 核对缓存 → 加载或构建 → 成功后发布。
        边界：不从 Session 恢复图；源码变化自然产生新代次，旧实体 ID 失效。
        """
        metadata = {
            "schema": "s18-relations-v1",
            "source": snapshot.fingerprint,
            "workspace": self.workspace.workspace_id,
            "parser": f"{sys.version_info.major}.{sys.version_info.minor}",
        }
        generation = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        destination = checked_cache_path(self.workspace.index_root / ("rel-" + generation))
        path = checked_cache_path(destination / "relations.sqlite")
        # s18 修改：s19 的关系扩展可复用同次召回目录；跨目标或跨版本必须拒绝。
        if catalog is None:
            catalog = EntityCatalog.parse(self.workspace.workspace_id, snapshot)
        if (
            catalog.workspace_id != self.workspace.workspace_id
            or catalog.fingerprint != snapshot.fingerprint
        ):
            raise ValueError("关系分析必须使用同目标、同版本实体目录")
        if destination.exists():
            return self._load(path, metadata, snapshot, catalog), False

        graph = RelationshipGraph.build(snapshot, catalog)
        self.workspace.index_root.mkdir(parents=True, exist_ok=True)
        # s18 新增：先把完整数据库写到本方法独占的临时目录；错误不会覆盖既有代次。
        with tempfile.TemporaryDirectory(
            prefix=".rel-build-", dir=self.workspace.index_root
        ) as temp:
            staging = Path(temp)
            # s18 修改：sqlite 的 with 只提交事务，不关闭连接；先关闭才能在 Windows 发布目录。
            with closing(sqlite3.connect(staging / "relations.sqlite")) as database, database:
                database.execute("CREATE TABLE metadata (value TEXT NOT NULL)")
                database.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata),))
                database.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                database.executemany(
                    "INSERT INTO nodes VALUES (?, ?)",
                    [(key, json.dumps(value)) for key, value in graph.nodes.items()],
                )
                database.execute("CREATE TABLE relations (value TEXT NOT NULL)")
                database.executemany(
                    "INSERT INTO relations VALUES (?)",
                    [(json.dumps(asdict(edge)),) for edge in graph.relations],
                )
            if (staging / "relations.sqlite").stat().st_size > MAX_GRAPH_BYTES:
                raise ValueError("关系缓存超过 64 MiB")
            # s18 新增：rename 发布目录后，TemporaryDirectory 只会清理已不存在的暂存名。
            staging.rename(destination)
        return graph, True

    def _load(self, path, metadata, snapshot, catalog):
        """有界加载普通 SQLite；不信任缺失节点或越界证据。"""
        checked_cache_path(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > MAX_GRAPH_BYTES:
            raise ValueError("关系缓存无效，请手动移走该代次后重建")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            if database.execute("SELECT value FROM metadata").fetchall() != [
                (json.dumps(metadata),)
            ]:
                raise ValueError("关系缓存版本不一致")
            rows = database.execute("SELECT id, value FROM nodes LIMIT 55001").fetchall()
            nodes = {key: json.loads(value) for key, value in rows}
            expected = {entity.entity_id: asdict(entity) for entity in catalog.entities}
            for key, node in nodes.items():
                if not isinstance(node, dict):
                    raise TypeError("关系节点必须是对象")
                if node.get("kind") == "module":
                    relative = node.get("path")
                    if relative not in snapshot.file_hashes:
                        raise ValueError("关系模块不在当前源码中")
                    identity = hashlib.sha256(
                        f"module:{relative}:{snapshot.file_hashes[relative]}".encode()
                    ).hexdigest()
                    if (
                        key != identity
                        or node.get("entity_id") != identity
                        or node.get("content_hash") != snapshot.file_hashes[relative]
                    ):
                        raise ValueError("关系模块版本无效")
                    if (
                        not relative.endswith(".py")
                        or relative in catalog.parse_errors
                        or node.get("name")
                        != relative[:-3].replace("/", ".").removesuffix(".__init__")
                        or node.get("qualified_name") != node.get("name")
                        or node.get("start_line") != 1
                        or node.get("end_line")
                        != max(1, len(source_lines(snapshot.file_contents[relative])))
                    ):
                        raise ValueError("关系模块元数据损坏")
                elif expected.get(key) != node:
                    raise ValueError("关系实体不属于当前版本")
            if not expected.keys() <= nodes.keys() or len(rows) > 55000:
                raise ValueError("关系节点缺失或超限")
            modules = {
                hashlib.sha256(
                    f"module:{relative}:{snapshot.file_hashes[relative]}".encode()
                ).hexdigest()
                for relative in snapshot.file_contents
                if relative.endswith(".py") and relative not in catalog.parse_errors
            }
            if nodes.keys() != expected.keys() | modules:
                raise ValueError("关系模块缺失或存在未知节点")
            rows = database.execute(
                "SELECT value FROM relations LIMIT ?", (MAX_RELATIONS + 1,)
            ).fetchall()
            if len(rows) > MAX_RELATIONS:
                raise ValueError("关系数量超限")
            relations = [Relation(**json.loads(value)) for (value,) in rows]
        line_counts = {
            relative: len(source_lines(data)) for relative, data in snapshot.file_contents.items()
        }
        for edge in relations:
            if (
                edge.source_id not in nodes
                or (edge.target_id is not None and edge.target_id not in nodes)
                or edge.kind not in {"calls", "imports", "inherits"}
                or edge.path not in line_counts
                or type(edge.line) is not int
                or not 1 <= edge.line <= line_counts[edge.path]
                or not isinstance(edge.expression, str)
                or len(edge.expression) > 200
                or not isinstance(edge.reason, str)
                or len(edge.reason) > 200
            ):
                raise ValueError("关系边或证据位置损坏")
        return RelationshipGraph(
            fingerprint=snapshot.fingerprint,
            nodes=nodes,
            relations=relations,
            parse_errors=catalog.parse_errors,
        )
