"""s17：独立的语义召回；文本、符号复用 s16，不提前实现跨路融合。"""

import json
import sqlite3
import uuid
from time import perf_counter

from s15_stateful_extensions.result import ToolResult
from s16_repository_search.cache import cache_directory_bytes
from s16_repository_search.entities import EntityCatalog
from s16_repository_search.evidence import CodeEvidence
from s16_repository_search.retrieval import CodeRetriever as SearchRetriever
from s17_semantic_retrieval.chunks import build_chunks
from s17_semantic_retrieval.semantic import SemanticIndex

MAX_RESULT_BYTES = 16 * 1024


# s17 新增：AST 只提供实体边界；自然语言匹配交给 SemanticIndex，不把两者混为同一算法。
class CodeRetriever(SearchRetriever):
    """在已有文本/符号搜索之外提供单路语义检索。

    输入：同一 workspace、可选 Zoekt 管理器、授权后创建的 SemanticIndex。
    输出：有文件版本与行范围的 ToolResult；embedding 指标不混入聊天用量。
    流程：text/symbol 交回 s16；semantic 扫描 → 结构切块 → 向量召回 → 证据裁剪 → 复核。
    边界：每次只选一种召回模式；没有 embedding 不静默降级；多路融合由 s19 实现。
    """

    def __init__(
        self, *, workspace, index_manager=None, semantic_index: SemanticIndex | None = None
    ):
        """仅保存同目标依赖，不扫描、创建缓存或请求模型。"""
        if index_manager is not None and index_manager.workspace is not workspace:
            raise ValueError("索引与检索必须共用同一 workspace")
        if semantic_index is not None and semantic_index.path.parent.parent != workspace.index_root:
            raise ValueError("语义缓存必须属于同一 workspace")
        self.workspace = workspace
        self.index_manager = index_manager
        self.semantic_index = semantic_index

    def search(
        self, query: str, scope: str = "", limit: int = 10, mode: str = "text"
    ) -> ToolResult:
        """显式选择单路检索；语义返回当前块，而不是缓存中的历史块。

        输入：查询、相对目录、文件上限、text/symbol/semantic 模式。
        输出：公共 files/hits 结果，语义命中附余弦分数；失败保留已发生的 embedding 用量。
        流程：验证参数 → 扫描当前源码 → 提取实体/切块 → 编码和查询 → 按文件显示 → 复核版本。
        边界：SQLite 保存哈希/向量，正文来自本次快照；结果和正文均有预算。
        """
        if mode in ("text", "symbol") and self.index_manager is not None:
            # 来自 s16：保持；引擎搜索、实体关联和安全补读直接复用，不重复扫描。
            return super().search(query, scope=scope, limit=limit, mode=mode)
        started = perf_counter()
        metrics = {
            "kind": "retrieval",
            "query_id": uuid.uuid4().hex,
            "workspace_id": self.workspace.workspace_id,
            "embedding": {},
        }
        payload = {"status": "invalid", "files": [], "truncated": False, "search_exhaustive": False}
        try:
            if not isinstance(mode, str) or mode not in {"text", "symbol", "semantic"}:
                raise ValueError("mode 必须是 text、symbol 或 semantic")
            self._validate(query, scope, limit)
            if mode != "semantic":
                raise ValueError("文本/符号通道未配置 Zoekt")
            if self.semantic_index is None:
                raise ValueError("语义能力未启用；需独立配置 embedding 并显式授权")
            payload.update(status="failed", mode=mode)
            # s17 新增：单次快照同时提供代码、实体身份和文件哈希。
            stage_started = perf_counter()
            snapshot = self.workspace.scan()
            metrics["scan_ms"] = (perf_counter() - stage_started) * 1000
            metrics["source_bytes"] = sum(map(len, snapshot.file_contents.values()))
            catalog = EntityCatalog.parse(self.workspace.workspace_id, snapshot)
            chunks = build_chunks(snapshot, catalog, scope=scope)
            metrics["chunks"] = len(chunks)
            payload.update(
                source_fingerprint=snapshot.fingerprint,
                index_generation=snapshot.fingerprint,
                indexed_files=len(snapshot.file_contents),
                skipped=snapshot.skipped_counts,
                python_parse_error_count=len(catalog.parse_errors),
            )
            stage_started = perf_counter()
            matches = self.semantic_index.search(
                chunks, query, scope=scope, limit=80, metrics=metrics["embedding"]
            )
            # s17 新增：保留不同块的位置；此处没有 RRF 或实体级跨路去重。
            by_path = {}
            for chunk, score in matches:
                if chunk.path not in by_path and len(by_path) >= limit:
                    payload["truncated"] = True
                    continue
                file = by_path.setdefault(
                    chunk.path,
                    {
                        "path": chunk.path,
                        "rank": len(by_path) + 1,
                        "content_hash": snapshot.file_hashes[chunk.path],
                        "hits": [],
                    },
                )
                if len(file["hits"]) >= 3:
                    payload["truncated"] = True
                    continue
                evidence = CodeEvidence(
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    snippet=chunk.text[:500],
                    content_hash=snapshot.file_hashes[chunk.path],
                    source="semantic",
                    entity=chunk.entity,
                    snippet_truncated=len(chunk.text) > 500,
                )
                file["hits"].append({**evidence.to_hit(), "similarity": score})
                payload["truncated"] |= evidence.snippet_truncated
            payload["files"] = list(by_path.values())
            metrics["search_ms"] = (perf_counter() - stage_started) * 1000
            metrics["channel_candidates"] = {"semantic": len(matches)}
            metrics["cache_bytes"] = (
                cache_directory_bytes(self.semantic_index.path.parent)
                if self.semantic_index.path.exists()
                else 0
            )
            # s17 新增：发送前重新核对源码；失败时不能登记旧块的补读指纹。
            stage_started = perf_counter()
            if self.workspace.compute_fingerprint() != snapshot.fingerprint:
                payload.update(status="stale", files=[], error="检索期间源码发生变化，请重新检索")
            else:
                payload["status"] = "ok" if payload["files"] else "empty"
            metrics["verify_ms"] = (perf_counter() - stage_started) * 1000
        except (
            ValueError,
            TypeError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            RecursionError,
        ) as error:
            payload.update(files=[], error=str(error)[:700])
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        while len(content.encode()) > MAX_RESULT_BYTES and payload["files"]:
            payload["files"].pop()
            payload["truncated"] = True
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
        if payload["status"] == "ok":
            for file in payload["files"]:
                self.workspace.search_hashes[file["path"]] = file["content_hash"]
        metrics.update(
            status=payload["status"],
            mode=mode if isinstance(mode, str) else "invalid",
            returned_files=len(payload["files"]),
            output_bytes=len(content.encode()),
            duration_ms=(perf_counter() - started) * 1000,
        )
        return ToolResult(
            ("Error: " if payload["status"] in {"invalid", "failed", "stale"} else "") + content,
            metrics,
        )
