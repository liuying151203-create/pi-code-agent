"""s18：内容寻址的向量缓存及小仓库精确余弦召回。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from s16_repository_search.workspace import checked_cache_path
from s18_semantic_retrieval.chunks import CodeChunk
from s18_semantic_retrieval.embeddings import EmbeddingProvider, EmbeddingResponseError, normalized

MAX_VECTOR_CACHE_BYTES = 128 * 1024 * 1024


# s18 新增：先做可解释的精确检索基线；不把 Qdrant/ColBERT 的效果当成本实现的效果。
class SemanticIndex:
    """缓存编码并为当前源码块排序，不把旧版本的块召回回来。

    输入：缓存根目录和显式授权的 provider；search 接收当前块、自然语言和 scope。
    输出：按余弦排序的块及独立 embedding 指标。
    流程：校验缓存 → 只编码缺失内容 → 编码查询 → 当前块内计算内积 → 返回前 K 块。
    边界：SQLite 只存哈希和向量；旧块缓存可复用，但不参与当前候选集合。
    """

    def __init__(self, *, index_root: Path, provider: EmbeddingProvider) -> None:
        """绑定独立模型身份；不创建目录、不调用服务。"""
        identity = hashlib.sha256(provider.cache_key.encode()).hexdigest()
        self.path = checked_cache_path(index_root / ("emb-" + identity) / "vectors.sqlite")
        self.provider = provider

    def search(
        self,
        chunks: list[CodeChunk],
        query: str,
        *,
        scope: str = "",
        limit: int = 40,
        metrics: dict[str, Any],
    ) -> list[tuple[CodeChunk, float]]:
        """为当前范围内的块准备向量并检索，失败不会返回看似成功的文本结果。

        输入：同版块、查询、目录、候选上限，以及本次请求独有的指标字典。
        输出：块/分数列表；metrics 保留已完成调用和未知用量次数。
        流程：读取缓存 → 分批编码缺失块并事务保存 → 查询编码 → 精确排名。
        边界：缓存命中不产生索引用量；失败请求的用量未知，不写成 0 Token。
        """
        metrics.update(
            requests=0,
            known_tokens=0,
            unknown_usage_requests=0,
            cache_hits=0,
            encoded_chunks=0,
            failed_requests=0,
        )
        # s18 新增：首次/增量编码与每次查询分别计数；不只报告一个混合总数。
        metrics["index"] = {"requests": 0, "known_tokens": 0, "unknown_usage_requests": 0}
        metrics["query"] = {"requests": 0, "known_tokens": 0, "unknown_usage_requests": 0}
        scoped = [chunk for chunk in chunks if not scope or chunk.path.startswith(scope + "/")]
        if not scoped:
            return []
        checked_cache_path(self.path)
        if self.path.exists() and (
            not self.path.is_file()
            or self.path.stat().st_nlink > 1
            or self.path.stat().st_size > MAX_VECTOR_CACHE_BYTES
        ):
            raise ValueError("向量缓存不是有效普通文件或超过 128 MiB")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # s18 新增：不让 SQLite 恢复/写入未知的预置 sidecar；异常遗留需用户单独检查。
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            checked_cache_path(sidecar)
            if sidecar.exists():
                raise ValueError("向量缓存存在 SQLite sidecar，请停止并发进程后检查，不自动覆盖")
        vectors: dict[str, list[float]] = {}
        # s18 修改：事务与连接生命周期分别管理，成功和失败都立即释放句柄。
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            # s18 新增：只读取当前内容键；内容哈希变化、删除和 scope 不靠旧清单判断。
            unique = {chunk.content_key: chunk for chunk in scoped}
            missing = []
            for key, chunk in unique.items():
                row = database.execute("SELECT value FROM vectors WHERE key=?", (key,)).fetchone()
                if row is None:
                    missing.append(chunk)
                else:
                    vectors[key] = normalized(json.loads(row[0]))
                    metrics["cache_hits"] += 1
            dimensions = {len(vector) for vector in vectors.values()}
            if len(dimensions) > 1:
                raise ValueError("向量缓存维度不一致，不调用服务覆盖损坏数据")
            expected_dimension = next(iter(dimensions), None)
            for start in range(0, len(missing), 16):
                batch = missing[start : start + 16]
                encoded = self._embed(
                    [chunk.embedding_input for chunk in batch], metrics, phase="index"
                )
                if expected_dimension is not None and len(encoded[0]) != expected_dimension:
                    raise ValueError("模型/缓存维度变化；请修改 EMBEDDING_REVISION 后重建")
                expected_dimension = len(encoded[0])
                # s18 新增：整批先校验再写，不能把半个响应当作完整索引。
                for chunk, vector in zip(batch, encoded, strict=True):
                    vectors[chunk.content_key] = vector
                    database.execute(
                        "INSERT INTO vectors VALUES (?, ?)",
                        (chunk.content_key, json.dumps(vector, allow_nan=False)),
                    )
                metrics["encoded_chunks"] += len(batch)
                # s18 修改：每批核对含未提交页面的实际容量，不能全部编码完才发现超限。
                pages = database.execute("PRAGMA page_count").fetchone()[0]
                page_size = database.execute("PRAGMA page_size").fetchone()[0]
                if pages * page_size > MAX_VECTOR_CACHE_BYTES:
                    raise ValueError("向量缓存超过 128 MiB，已停止继续编码并回滚本轮写入")
            query_vector = self._embed([query], metrics, phase="query")[0]
            if any(len(vector) != len(query_vector) for vector in vectors.values()):
                raise ValueError("模型/缓存维度变化；请修改 EMBEDDING_REVISION 后重建")
        if self.path.stat().st_size > MAX_VECTOR_CACHE_BYTES:
            raise ValueError("向量缓存超过 128 MiB，请缩小目标或手动清理本模型缓存")
        ranked = [
            (
                chunk,
                sum(a * b for a, b in zip(vectors[chunk.content_key], query_vector, strict=True)),
            )
            for chunk in scoped
        ]
        return sorted(ranked, key=lambda item: (-item[1], item[0].path, item[0].start_line))[:limit]

    def _embed(self, texts: list[str], metrics: dict[str, Any], *, phase: str) -> list[list[float]]:
        """统一校验可注入 provider 的响应；失败与未知用量保留在当前指标。"""
        metrics["requests"] += 1
        known_before = metrics["known_tokens"]
        unknown_before = metrics["unknown_usage_requests"]
        usage_reported = False
        try:
            batch = self.provider.embed(texts)
            if batch.tokens is None:
                metrics["unknown_usage_requests"] += 1
            elif type(batch.tokens) is int and batch.tokens >= 0:
                metrics["known_tokens"] += batch.tokens
            else:
                raise ValueError("embedding 用量无效")
            usage_reported = True
            if len(batch.vectors) != len(texts):
                raise ValueError("embedding 返回数量不匹配")
            vectors = [normalized(vector) for vector in batch.vectors]
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("embedding 批次维度不一致")
            return vectors
        except Exception as error:
            # s18 新增：调用失败也可能已经计费；没有响应的请求记未知，不伪造 Token。
            metrics["failed_requests"] += 1
            if not usage_reported:
                if isinstance(error, EmbeddingResponseError) and error.known_tokens is not None:
                    metrics["known_tokens"] += error.known_tokens
                else:
                    metrics["unknown_usage_requests"] += 1
            raise
        finally:
            metrics[phase]["requests"] += 1
            metrics[phase]["known_tokens"] += metrics["known_tokens"] - known_before
            metrics[phase]["unknown_usage_requests"] += (
                metrics["unknown_usage_requests"] - unknown_before
            )
