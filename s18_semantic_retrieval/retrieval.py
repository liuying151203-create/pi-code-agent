"""s18：同版源码上的文本、符号、语义召回及 RRF 融合。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any

from s15_stateful_extensions.result import ToolResult
from s16_repository_search.cache import cache_directory_bytes
from s17_repository_symbols.entities import CodeEntity, EntityCatalog
from s17_repository_symbols.index import RepositoryIndex
from s17_repository_symbols.retrieval import CodeRetriever as SymbolRetriever
from s18_semantic_retrieval.chunks import build_chunks
from s18_semantic_retrieval.semantic import SemanticIndex
from s18_semantic_retrieval.workspace import RepositoryWorkspace

MAX_RESULT_BYTES = 16 * 1024


# s18 新增：统一的是“同代代码位置”，不是直接相加余弦分数和 Zoekt 分数。
@dataclass
class Candidate:
    """承载可复核的位置与各路名次。

    输入：路径、行范围、片段、实体及通道名次；输出：用于 RRF 排序的候选。
    流程：各路转为 Candidate → 以实体/位置去重 → 融合 → 按文件组织结果。
    边界：长函数多个块合并到同一实体；模块块用起始行区分。
    """

    path: str
    start_line: int
    end_line: int
    snippet: str
    entity: CodeEntity | None
    channel_ranks: dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """实体 ID 已包含文件版本；无实体的位置只在当前快照内使用。"""
        return self.entity.entity_id if self.entity else f"{self.path}:{self.start_line}"

    @property
    def score(self) -> float:
        """RRF 的常数 60 为公开基线，不宣称已经过本项目评测调优。"""
        return sum(1 / (60 + rank) for rank in self.channel_ranks.values())


def fuse_candidates(channels: dict[str, list[Candidate]]) -> list[Candidate]:
    """将不同召回通道合成一个有来源的排名，不比较原始分数。

    输入：通道名到有序候选的映射；输出：按 RRF 分数排序的去重候选。
    流程：每路按实体/位置去重并记录名次 → 跨路合并 → 分数降序、路径/行号稳定破同分。
    边界：同一路多个关键词或长函数多个块只贡献一次；不在这里决定文件配额。
    """
    merged: dict[str, Candidate] = {}
    for channel, candidates in channels.items():
        seen = set()
        for candidate in candidates:
            if candidate.key in seen:
                continue
            seen.add(candidate.key)
            target = merged.setdefault(candidate.key, candidate)
            target.channel_ranks[channel] = len(seen)
    return sorted(merged.values(), key=lambda item: (-item.score, item.path, item.start_line))


# 来自 s17：保持；参数/位置核验复用，search 主流程在 s18 显式展开。
class CodeRetriever(SymbolRetriever):
    """用一次源码快照完成多路召回，返回有来源、有版本的有界证据。

    输入：同一 workspace 的可选 Zoekt 管理器和可选 SemanticIndex。
    输出：ToolResult，正文是结果，details 是检索/embedding 的独立统计。
    流程：校验 → 扫描 → 同代实体 → 各路召回 → RRF → 源码复核 → 裁剪/登记。
    边界：semantic 不依赖 Zoekt；text/symbol 不调用 embedding；hybrid 故障不静默降级。
    """

    def __init__(
        self,
        *,
        workspace: RepositoryWorkspace,
        index_manager: RepositoryIndex | None = None,
        semantic_index: SemanticIndex | None = None,
    ) -> None:
        """保存可见依赖，不在构造时扫描或联网；跨目标组装立即拒绝。"""
        if index_manager is not None and index_manager.workspace is not workspace:
            raise ValueError("索引与检索必须共用同一 workspace")
        if semantic_index is not None and semantic_index.path.parent.parent != workspace.index_root:
            raise ValueError("语义缓存必须属于同一 workspace")
        self.workspace = workspace
        self.index_manager = index_manager
        self.semantic_index = semantic_index

    def search(
        self,
        query: str,
        scope: str = "",
        limit: int = 10,
        mode: str = "text",
        keywords: list[str] | None = None,
    ) -> ToolResult:
        """执行一次定位请求，而不是让模型逐文件猜测。

        输入：query 为字面文本或自然语言；hybrid 另用 keywords 指定 1–4 个代码关键词。
        输出：去重文件、候选通道名次、实体、源码指纹和清晰失败状态。
        流程：先固定源码/目录 → 各通道在该版本上查询 → 融合 → 复核 → 返回。
        边界：不使用聊天模型改写关键词，不把 embedding 用量混入聊天统计。
        """
        started = perf_counter()
        metrics: dict[str, Any] = {
            "kind": "retrieval",
            "query_id": uuid.uuid4().hex,
            "workspace_id": self.workspace.workspace_id,
            "embedding": {},
        }
        payload: dict[str, Any] = {
            "status": "invalid",
            "files": [],
            "truncated": False,
            "search_exhaustive": False,
        }
        try:
            self._validate(query, scope, limit)
            if not isinstance(mode, str) or mode not in {"text", "symbol", "semantic", "hybrid"}:
                raise ValueError("mode 必须为 text、symbol、semantic 或 hybrid")
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
            if mode in {"semantic", "hybrid"} and self.semantic_index is None:
                raise ValueError("语义能力未启用；需独立配置 embedding 并显式授权")
            if mode != "semantic" and self.index_manager is None:
                raise ValueError("文本/符号通道未配置 Zoekt")
            payload.update(status="failed", mode=mode)

            # s18 修改：一次扫描服务全部通道，不能调用三次 s17.search 得到三个版本。
            stage_started = perf_counter()
            snapshot = self.workspace.scan()
            metrics["scan_ms"] = (perf_counter() - stage_started) * 1000
            metrics["source_bytes"] = sum(map(len, snapshot.file_contents.values()))
            stage_started = perf_counter()
            if mode == "semantic":
                catalog = EntityCatalog.parse(self.workspace.workspace_id, snapshot)
                generation = snapshot.fingerprint
                prepared_index = None
            else:
                prepared_index = self.index_manager.prepare(snapshot)
                catalog = prepared_index.entity_catalog
                generation = prepared_index.generation
                metrics["rebuilt"] = prepared_index.rebuilt
            metrics["prepare_ms"] = (perf_counter() - stage_started) * 1000
            metrics["generation"] = generation
            payload.update(
                source_fingerprint=snapshot.fingerprint,
                index_generation=generation,
                indexed_files=len(snapshot.file_contents),
                skipped=snapshot.skipped_counts,
                python_parse_error_count=len(catalog.parse_errors),
            )
            channels: dict[str, list[Candidate]] = {}
            backend_exhaustive = True
            stage_started = perf_counter()

            # s18 新增：语义直接枚举当前块，不会把缓存里已删除的源码再次召回。
            if mode in {"semantic", "hybrid"}:
                chunks = build_chunks(snapshot, catalog, scope=scope)
                semantic_matches = self.semantic_index.search(
                    chunks, query, scope=scope, limit=80, metrics=metrics["embedding"]
                )
                channels["semantic"] = [
                    Candidate(
                        chunk.path, chunk.start_line, chunk.end_line, chunk.text, chunk.entity
                    )
                    for chunk, _ in semantic_matches
                ]
                metrics["chunks"] = len(chunks)

            # 来自 s17：保持；原引擎负责 text/symbol，返回位置仍由同版源码核验。
            if prepared_index is not None:
                modes = ("text", "symbol") if mode == "hybrid" else (mode,)
                queries = list(dict.fromkeys(keywords)) if mode == "hybrid" else [query]
                for channel in modes:
                    candidates: list[Candidate] = []
                    for literal in queries:
                        matches = (
                            self.index_manager.backend.search_index(
                                prepared_index.index_dir, literal, scope, 20, mode=channel
                            )
                            if snapshot.file_contents
                            else {"files": [], "exhaustive": True, "truncated": False}
                        )
                        backend_exhaustive = backend_exhaustive and matches["exhaustive"]
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
                                candidates.append(
                                    Candidate(
                                        file["path"],
                                        hit["start_line"],
                                        hit["end_line"],
                                        hit["snippet"],
                                        entity,
                                    )
                                )
                    channels[channel] = candidates

            # s18 新增：RRF 只合并通道名次，最终按文件去重，防止一个大文件挤满结果。
            ranked = fuse_candidates(channels)
            by_path: dict[str, dict[str, Any]] = {}
            for candidate in ranked:
                if candidate.path not in by_path and len(by_path) >= limit:
                    payload["truncated"] = True
                    continue
                file = by_path.setdefault(
                    candidate.path,
                    {
                        "path": candidate.path,
                        "rank": len(by_path) + 1,
                        "content_hash": snapshot.file_hashes[candidate.path],
                        "hits": [],
                    },
                )
                if len(file["hits"]) >= 3:
                    payload["truncated"] = True
                    continue
                file["hits"].append(
                    {
                        "start_line": candidate.start_line,
                        "end_line": candidate.end_line,
                        "snippet": candidate.snippet[:500],
                        "snippet_truncated": len(candidate.snippet) > 500,
                        "entity": asdict(candidate.entity) if candidate.entity else None,
                        "channel_ranks": candidate.channel_ranks,
                        "rrf_score": candidate.score,
                    }
                )
            payload["files"] = list(by_path.values())
            # s18 修改：片段显示省略也必须反映到总结果，不能只标单个字段。
            payload["truncated"] |= any(
                hit["snippet_truncated"] for file in payload["files"] for hit in file["hits"]
            )
            metrics["search_ms"] = (perf_counter() - stage_started) * 1000
            metrics["channel_candidates"] = {
                channel: len(items) for channel, items in channels.items()
            }
            # 来自 s17：保持；仅统计实际使用的代次，不为观测重读源码或扫描所有旧缓存。
            stage_started = perf_counter()
            cache_bytes = (
                cache_directory_bytes(prepared_index.index_dir.parent) if prepared_index else 0
            )
            if mode in {"semantic", "hybrid"} and self.semantic_index.path.exists():
                cache_bytes += cache_directory_bytes(self.semantic_index.path.parent)
            metrics["cache_bytes"] = cache_bytes
            metrics["cache_stat_ms"] = (perf_counter() - stage_started) * 1000
            # 来自 s17：保持；结束复核不保存第二份正文；旧结果不能登记成当前证据。
            stage_started = perf_counter()
            latest_fingerprint = self.workspace.compute_fingerprint()
            metrics["verify_ms"] = (perf_counter() - stage_started) * 1000
            if latest_fingerprint != snapshot.fingerprint:
                payload.update(status="stale", files=[], error="检索期间源码发生变化，请重新检索")
            else:
                payload["status"] = (
                    ("ok" if payload["files"] else "empty") if backend_exhaustive else "partial"
                )
                # s18 修改：语义是有界 Top-K，不声称穷举了全部相关代码。
                payload["search_exhaustive"] = backend_exhaustive and mode in {"text", "symbol"}
        except (
            ValueError,
            TypeError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
            RecursionError,
        ) as error:
            payload.update(files=[], error=str(error)[:700], search_exhaustive=False)

        content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        while len(content.encode()) > MAX_RESULT_BYTES and payload["files"]:
            payload["files"].pop()
            payload["truncated"] = True
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if payload["status"] in {"ok", "partial"}:
            for file in payload["files"]:
                self.workspace.search_hashes[file["path"]] = file["content_hash"]
        metrics.update(
            status=payload["status"],
            mode=payload.get("mode", "invalid"),
            returned_files=len(payload["files"]),
            output_bytes=len(content.encode()),
            duration_ms=(perf_counter() - started) * 1000,
        )
        if payload["status"] in {"invalid", "failed", "stale"}:
            content = "Error: " + content
        return ToolResult(content, metrics)
