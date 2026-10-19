"""s19：用实体 ID 在当前版本中查询一跳关系，返回可补读的证据。"""

import json
import sqlite3
import uuid
from dataclasses import asdict
from time import perf_counter

from s15_stateful_extensions.result import ToolResult
from s16_repository_search.workspace import source_lines
from s19_relationship_navigation.index import RelationshipIndex
from s19_relationship_navigation.workspace import RepositoryWorkspace


# s19 新增：关系导航与 s18 的召回并列，而不是偷偷修改核心循环或再次摘要源码。
class RelationshipRetriever:
    """将 s18 候选实体扩展为有证据的关联代码。

    输入：同目标 workspace、RelationshipIndex 和实体 ID/方向/种类/上限。
    输出：ToolResult：节点、关系、未解析说明，以及独立检索指标。
    流程：扫描 → 准备当前图 → 单跳查询 → 组织证据 → 复核 → 登记补读指纹。
    边界：不递归遍历；不把同名函数连在一起；不请求模型或 embedding。
    """

    def __init__(
        self, *, workspace: RepositoryWorkspace, relationship_index: RelationshipIndex
    ) -> None:
        """拒绝跨目标组装；构造时不读取或写入文件。"""
        if relationship_index.workspace is not workspace:
            raise ValueError("关系索引和检索必须共用 workspace")
        self.workspace = workspace
        self.relationship_index = relationship_index

    def related(
        self, entity_id: str, direction: str = "incoming", kind: str = "calls", limit: int = 10
    ) -> ToolResult:
        """按当前实体导航；旧会话里的旧 ID 不能解释成新源码。

        输入：search_code 返回的 entity_id；输出：一跳关系及双方节点、证据行。
        流程：参数校验 → 同代图 → 查询 → 位置证据 → 指纹复核 → 有界输出。
        边界：unresolved 仅在 outgoing 可见，不可能倒推出一个未知被调用者。
        """
        started = perf_counter()
        payload = {"status": "invalid", "relations": [], "truncated": False}
        metrics = {
            "kind": "retrieval",
            "query_id": uuid.uuid4().hex,
            "mode": "relations",
            "workspace_id": self.workspace.workspace_id,
        }
        try:
            if (
                not isinstance(entity_id, str)
                or len(entity_id) != 64
                or any(char not in "0123456789abcdef" for char in entity_id)
            ):
                raise ValueError("entity_id 必须来自当前 search_code 的实体结果")
            # s19 新增：查询参数在建图前检查，非法调用不触发扫描/索引写入。
            if direction not in {"incoming", "outgoing"} or kind not in {
                "calls",
                "imports",
                "inherits",
                "all",
            }:
                raise ValueError("关系方向或种类无效")
            if type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("limit 必须为 1–20")
            payload["status"] = "failed"
            snapshot = self.workspace.scan()
            graph, rebuilt = self.relationship_index.prepare(snapshot)
            edges, truncated = graph.neighbors(
                entity_id, direction=direction, kind=kind, limit=limit
            )
            payload.update(
                entity=graph.nodes[entity_id],
                source_fingerprint=snapshot.fingerprint,
                direction=direction,
                kind=kind,
                truncated=truncated,
                python_parse_error_count=len(graph.parse_errors),
            )
            evidence_lines = {
                edge.path: source_lines(snapshot.file_contents[edge.path]) for edge in edges
            }
            for edge in edges:
                evidence = evidence_lines[edge.path][edge.line - 1]
                payload["relations"].append(
                    {
                        **asdict(edge),
                        "source": graph.nodes[edge.source_id],
                        "target": graph.nodes.get(edge.target_id),
                        "evidence": evidence[:500],
                        "evidence_truncated": len(evidence) > 500,
                        "content_hash": snapshot.file_hashes[edge.path],
                    }
                )
            if self.workspace.compute_fingerprint() != snapshot.fingerprint:
                payload.update(status="stale", relations=[], error="查询期间源码变化，请重新检索")
            else:
                payload["status"] = "ok" if edges else "empty"
            metrics.update(
                rebuilt=rebuilt,
                graph_relations=len(graph.relations),
                unresolved=sum(edge.target_id is None for edge in graph.relations),
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            RecursionError,
        ) as error:
            payload.update(relations=[], error=str(error)[:700])
        content = json.dumps(payload, ensure_ascii=False)
        while len(content.encode()) > 16 * 1024 and payload["relations"]:
            payload["relations"].pop()
            payload["truncated"] = True
            content = json.dumps(payload, ensure_ascii=False)
        # s19 新增：极长符号名也受总预算约束，不能在没有边时绕过上限。
        if len(content.encode()) > 16 * 1024:
            payload = {
                "status": "failed",
                "relations": [],
                "truncated": True,
                "error": "实体元数据超过 16 KiB 结果预算，未返回超大正文",
            }
            content = json.dumps(payload, ensure_ascii=False)
        if payload["status"] in {"ok", "empty"}:
            paths = {item["path"] for item in payload["relations"]}
            paths.add(payload["entity"]["path"])
            for item in payload["relations"]:
                paths.add(item["source"]["path"])
                if item["target"]:
                    paths.add(item["target"]["path"])
            for path in paths:
                self.workspace.search_hashes[path] = snapshot.file_hashes[path]
        metrics.update(
            status=payload["status"],
            returned_relations=len(payload["relations"]),
            duration_ms=(perf_counter() - started) * 1000,
            output_bytes=len(content.encode()),
        )
        return ToolResult(
            ("Error: " if payload["status"] in {"invalid", "failed", "stale"} else "") + content,
            metrics,
        )
