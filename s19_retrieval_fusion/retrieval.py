"""s19：同版多路召回编排；证据算法独立在 evidence.py，关系解析复用 s18。"""

import json
import sqlite3
import subprocess
import uuid
from dataclasses import asdict
from time import perf_counter

from s15_stateful_extensions.result import ToolResult
from s16_repository_search.cache import cache_directory_bytes
from s16_repository_search.entities import CodeEntity, EntityCatalog
from s16_repository_search.evidence import CodeEvidence
from s16_repository_search.workspace import source_lines
from s17_semantic_retrieval.chunks import build_chunks
from s17_semantic_retrieval.retrieval import CodeRetriever as SingleChannelRetriever
from s19_retrieval_fusion.evidence import (
    MAX_RESULT_BYTES,
    Candidate,
    EvidencePipeline,
    FusionPolicy,
)


# s19 新增：检索器只组织一次请求；排名与显示预算通过独立组件注入。
class CodeRetriever(SingleChannelRetriever):
    """在同一源码版本上完成召回、可选关系扩展和证据组装。

    输入：同目标 workspace、可选 Zoekt/SemanticIndex、EvidencePipeline、可选关系索引。
    输出：files 是主要命中，related_context 是有来源的一跳关系；details 单独统计开销。
    流程：校验 → 一次快照/实体目录 → 各路召回 → pipeline.rank → 可选关系 → pack → 复核。
    边界：不调用聊天模型改写关键词；任何通道失败均明确报错，不以空列表掩盖故障。
    """

    def __init__(
        self,
        *,
        workspace,
        index_manager=None,
        semantic_index=None,
        evidence_pipeline=None,
        relationship_index=None,
    ):
        """检查目标一致性；默认策略只服务当前行为，main 仍显式组装管线。"""
        super().__init__(
            workspace=workspace, index_manager=index_manager, semantic_index=semantic_index
        )
        if relationship_index is not None and relationship_index.workspace is not workspace:
            raise ValueError("关系索引必须与召回共用 workspace")
        self.evidence_pipeline = (
            evidence_pipeline if evidence_pipeline is not None else EvidencePipeline(FusionPolicy())
        )
        self.relationship_index = relationship_index

    def search(
        self,
        query: str,
        scope: str = "",
        limit: int = 10,
        mode: str = "text",
        keywords: list[str] | None = None,
        include_related: bool = False,
    ) -> ToolResult:
        """执行一次多路查询，不在上层隐藏回调或递归工具调用。

        输入：查询与模式；hybrid 必须给代码关键词；include_related 显式启用一跳补充。
        输出：统一证据、每路覆盖说明和预算省略原因。
        流程：扫描 → 准备同版索引 → 召回 → 聚合排名 → 关系补充 → 组装 → 最终版本复核。
        边界：文本/符号不触发 embedding；semantic 无需 Zoekt；关系不是第四张相关度排名表。
        """
        started = perf_counter()
        metrics = {
            "kind": "retrieval",
            "query_id": uuid.uuid4().hex,
            "workspace_id": self.workspace.workspace_id,
            "embedding": {},
        }
        payload = {"status": "invalid", "files": [], "truncated": False, "search_exhaustive": False}
        try:
            if not isinstance(mode, str) or mode not in {"text", "symbol", "semantic", "hybrid"}:
                raise ValueError("mode 必须是 text、symbol、semantic 或 hybrid")
            # s19 新增：hybrid 单独校验意图与关键词；错误调用不能触发扫描或远程发送。
            self._validate(query, scope, limit)
            if keywords is not None and (
                not isinstance(keywords, list)
                or not 1 <= len(keywords) <= 4
                or any(
                    not isinstance(word, str)
                    or not word.strip()
                    or len(word) > 128
                    or any(char in word for char in "\r\n\x00")
                    for word in keywords
                )
            ):
                raise ValueError("keywords 必须是 1–4 个单行代码关键词")
            if mode == "hybrid" and not keywords:
                raise ValueError("hybrid 需要 keywords；query 描述需求，keywords 搜代码名称/正文")
            if mode != "hybrid" and keywords is not None:
                raise ValueError("keywords 只用于 hybrid")
            if type(include_related) is not bool:
                raise ValueError("include_related 必须是布尔值")
            if include_related and self.relationship_index is None:
                raise ValueError("关系扩展未配置")
            if mode in {"semantic", "hybrid"} and self.semantic_index is None:
                raise ValueError("语义能力未启用；需独立配置 embedding 并显式授权")
            if mode != "semantic" and self.index_manager is None:
                raise ValueError("文本/符号通道未配置 Zoekt")
            payload.update(status="failed", mode=mode)
            # s19 新增：全部通道及关系补充共用这一次快照，避免混合检索期间的两个版本。
            stage_started = perf_counter()
            snapshot = self.workspace.scan()
            metrics["scan_ms"] = (perf_counter() - stage_started) * 1000
            metrics["source_bytes"] = sum(map(len, snapshot.file_contents.values()))
            stage_started = perf_counter()
            prepared_index = self.index_manager.prepare(snapshot) if mode != "semantic" else None
            catalog = (
                prepared_index.entity_catalog
                if prepared_index is not None
                else EntityCatalog.parse(self.workspace.workspace_id, snapshot)
            )
            metrics["prepare_ms"] = (perf_counter() - stage_started) * 1000
            if prepared_index is not None:
                metrics["rebuilt"] = prepared_index.rebuilt
            generation = prepared_index.generation if prepared_index else snapshot.fingerprint
            payload.update(
                source_fingerprint=snapshot.fingerprint,
                index_generation=generation,
                indexed_files=len(snapshot.file_contents),
                skipped=snapshot.skipped_counts,
                python_parse_error_count=len(catalog.parse_errors),
            )
            channels = {}
            coverage = {}
            backend_exhaustive = True
            stage_started = perf_counter()
            # 来自 s17：保持；结构切块和向量缓存独立于融合算法。
            if mode in {"semantic", "hybrid"}:
                chunks = build_chunks(snapshot, catalog, scope=scope)
                matches = self.semantic_index.search(
                    chunks, query, scope=scope, limit=80, metrics=metrics["embedding"]
                )
                channels["semantic"] = [
                    Candidate(
                        CodeEvidence(
                            path=chunk.path,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            snippet=chunk.text,
                            content_hash=snapshot.file_hashes[chunk.path],
                            source="semantic",
                            entity=chunk.entity,
                        ),
                        similarity=score,
                    )
                    for chunk, score in matches
                ]
                coverage["semantic"] = {"status": "top_k", "candidates": len(matches)}
                metrics["chunks"] = len(chunks)
            # 来自 s16：保持；只接受引擎给出的同版位置，不重新发明文本/符号索引。
            if prepared_index is not None:
                modes = ("text", "symbol") if mode == "hybrid" else (mode,)
                # s19 修改：关键词使用稳定顺序，避免仅交换参数顺序就改变同一通道的排名。
                queries = sorted(set(keywords)) if mode == "hybrid" else [query]
                for channel in modes:
                    candidates = []
                    channel_complete = True
                    for literal in queries:
                        matches = (
                            self.index_manager.backend.search_index(
                                prepared_index.index_dir, literal, scope, 20, mode=channel
                            )
                            if snapshot.file_contents
                            else {"files": [], "exhaustive": True, "truncated": False}
                        )
                        channel_complete &= matches["exhaustive"]
                        payload["truncated"] |= matches["truncated"]
                        files = self._build_file_results(
                            matches["files"],
                            snapshot,
                            literal,
                            scope,
                            20,
                            entity_catalog=catalog,
                            mode=channel,
                        )
                        for file in files:
                            for hit in file["hits"]:
                                entity = CodeEntity(**hit["entity"]) if hit["entity"] else None
                                evidence = CodeEvidence(
                                    path=file["path"],
                                    start_line=hit["start_line"],
                                    end_line=hit["end_line"],
                                    snippet=hit["snippet"],
                                    content_hash=file["content_hash"],
                                    source=channel,
                                    entity=entity,
                                    snippet_truncated=hit["snippet_truncated"],
                                )
                                # s19 新增：交错各关键词的名次，不让首个关键词的全部结果占前排。
                                candidates.append(
                                    (
                                        file["rank"],
                                        file["path"],
                                        hit["start_line"],
                                        literal,
                                        Candidate(evidence),
                                    )
                                )
                    candidates.sort(key=lambda item: item[:-1])
                    channels[channel] = [item[-1] for item in candidates]
                    backend_exhaustive &= channel_complete
                    coverage[channel] = {
                        "status": "complete" if channel_complete else "partial",
                        "candidates": len(candidates),
                    }
            # s19 新增：上层直接呈现 rank → 可选扩展 → pack，不把关键过程藏在工具回调中。
            ranked = self.evidence_pipeline.rank(channels)
            # s19 新增：关系只是主要实体的上下文补充，不参与 RRF 投票。
            related_context = []
            if include_related:
                graph, rebuilt = self.relationship_index.prepare(snapshot, catalog=catalog)
                metrics["relationship_rebuilt"] = rebuilt
                seen_edges = set()
                seeds = [
                    item.matches[0].evidence.entity
                    for item in ranked
                    if item.matches[0].evidence.entity is not None
                ][:3]
                for seed in seeds:
                    edges, omitted = graph.neighbors(
                        seed.entity_id, direction="incoming", kind="calls", limit=5, scope=scope
                    )
                    payload["truncated"] |= omitted
                    for edge in edges:
                        key = (edge.source_id, edge.target_id, edge.path, edge.line, edge.kind)
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        line = source_lines(snapshot.file_contents[edge.path])[edge.line - 1]
                        related_context.append(
                            {
                                **asdict(edge),
                                "source": graph.nodes[edge.source_id],
                                "target": graph.nodes.get(edge.target_id),
                                "seed_id": seed.entity_id,
                                "evidence": line[:500],
                                "evidence_truncated": len(line) > 500,
                                "content_hash": snapshot.file_hashes[edge.path],
                                "reason": "主要命中实体的直接调用者",
                            }
                        )
                coverage["relations"] = {"status": "bounded", "candidates": len(related_context)}
            payload["coverage"] = coverage
            payload = self.evidence_pipeline.pack(
                payload, ranked, snapshot, limit=limit, relations=related_context
            )
            metrics["search_ms"] = (perf_counter() - stage_started) * 1000
            metrics["channel_candidates"] = {key: len(items) for key, items in channels.items()}
            metrics["generation"] = generation
            metrics["cache_bytes"] = (
                cache_directory_bytes(prepared_index.index_dir.parent)
                if prepared_index is not None
                else 0
            )
            if (
                self.semantic_index is not None
                and mode in {"semantic", "hybrid"}
                and self.semantic_index.path.exists()
            ):
                metrics["cache_bytes"] += cache_directory_bytes(self.semantic_index.path.parent)
            stage_started = perf_counter()
            if self.workspace.compute_fingerprint() != snapshot.fingerprint:
                payload.update(
                    status="stale",
                    files=[],
                    related_context=[],
                    error="检索期间源码发生变化，请重新检索",
                )
            else:
                payload["status"] = (
                    ("ok" if payload["files"] else "empty") if backend_exhaustive else "partial"
                )
                payload["search_exhaustive"] = backend_exhaustive and mode in {"text", "symbol"}
            metrics["verify_ms"] = (perf_counter() - stage_started) * 1000
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
            RecursionError,
        ) as error:
            payload.update(
                files=[], related_context=[], error=str(error)[:700], search_exhaustive=False
            )
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(content.encode()) > MAX_RESULT_BYTES:
            payload = {
                "status": "failed",
                "files": [],
                "truncated": True,
                "search_exhaustive": False,
                "error": "元数据超过结果预算",
            }
            content = json.dumps(payload, ensure_ascii=False)
        if payload["status"] in {"ok", "partial"}:
            paths = {file["path"] for file in payload["files"]}
            for edge in payload.get("related_context", []):
                paths.add(edge["path"])
                paths.add(edge["source"]["path"])
                if edge["target"]:
                    paths.add(edge["target"]["path"])
            for path in paths:
                self.workspace.search_hashes[path] = snapshot.file_hashes[path]
        metrics.update(
            status=payload["status"],
            mode=payload.get("mode", "invalid"),
            returned_files=len(payload["files"]),
            output_bytes=len(content.encode()),
            duration_ms=(perf_counter() - started) * 1000,
        )
        return ToolResult(
            ("Error: " if payload["status"] in {"invalid", "failed", "stale"} else "") + content,
            metrics,
        )
