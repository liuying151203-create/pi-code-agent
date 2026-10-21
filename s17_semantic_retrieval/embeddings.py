"""s17：独立 embedding 协议和 OpenAI 兼容适配器，不复用聊天模型配置。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from openai import OpenAI, OpenAIError


# s17 新增：测试替身和真实服务实现同一协议；不是对聊天 ModelProvider 的继承。
@dataclass(frozen=True)
class EmbeddingBatch:
    """承载一次编码的有序向量和可选真实 Token 数；未知用量用 None 表示。"""

    vectors: list[list[float]]
    tokens: int | None


# s17 新增：响应无效不等于未计费；校验异常也携带服务已经报告的用量。
class EmbeddingResponseError(ValueError):
    """保留响应校验失败的已知 Token；无报告时仍为 None，不推算费用。"""

    def __init__(self, message: str, *, known_tokens: int | None) -> None:
        super().__init__(message)
        self.known_tokens = known_tokens


class EmbeddingProvider(Protocol):
    """声明语义索引实际需要的接口。

    输入：一批非空文本；输出：顺序一致的向量与服务报告的用量。
    流程：SemanticIndex 注入实现 → embed；cache_key 区分端点、模型、维度及版本。
    边界：不负责源码扫描、聊天回答或向量持久化。
    """

    cache_key: str

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        """编码一批文本，响应缺失、错序或向量无效时拒绝。"""
        ...


def normalized(vector: list[float]) -> list[float]:
    """校验数值与非零范数，再单位化；余弦内积由此具有统一尺度。"""
    if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
        raise ValueError("embedding 必须是 1–8192 维有限数值向量")
    try:
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise ValueError("embedding 必须是有限数值，不能包含布尔值")
    except OverflowError as error:
        raise ValueError("embedding 数值超出浮点表示范围") from error
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding 向量范数必须非零且有限")
    return [value / norm for value in vector]


# s17 新增：兼容接口只复用请求协议，不假定 DeepSeek 聊天服务支持 embedding。
class OpenAIEmbeddingProvider:
    """将独立配置转换为 /embeddings 请求。

    输入：端点、模型、凭证、可选维度和模型修订号；输出：EmbeddingBatch。
    流程：校验配置 → 创建客户端（不联网）→ embed 时请求并按 response.index 排序。
    边界：构造此对象不代表授权；main 仅在用户显式启用时构造，不自动下载模型。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        dimensions: int | None = None,
        revision: str = "1",
    ) -> None:
        """校验独立配置并计算非秘密缓存身份，不发送任何源码。"""
        endpoint = urlsplit(base_url)
        if (
            not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.scheme not in {"https", "http"}
            or (
                endpoint.scheme == "http"
                and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("EMBEDDING_BASE_URL 必须为 HTTPS 或本地 HTTP，无凭证/查询参数")
        if not model.strip() or not api_key.strip() or not revision.strip():
            raise ValueError("embedding 模型、API key、修订号不能为空")
        if dimensions is not None and (type(dimensions) is not int or not 1 <= dimensions <= 8192):
            raise ValueError("EMBEDDING_DIMENSIONS 必须为 1–8192；不支持时留空")
        identity = ["s17-chunks-v1", base_url.rstrip("/"), model, dimensions, revision]
        self.cache_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.model = model
        self.dimensions = dimensions
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=60, max_retries=0)

    @classmethod
    def from_env(cls) -> OpenAIEmbeddingProvider:
        """读取 EMBEDDING_*，不回退 MODEL_*；授权标志由运行入口检查。"""
        dimensions = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
        return cls(
            base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            model=os.getenv("EMBEDDING_MODEL", ""),
            api_key=os.getenv("EMBEDDING_API_KEY", ""),
            dimensions=int(dimensions) if dimensions else None,
            revision=os.getenv("EMBEDDING_REVISION", "1"),
        )

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        """请求一批向量；核对 index、条数、维度和用量后按输入顺序返回。

        输入：最多 16 条非空文本；输出：与输入一一对应的单位向量和真实用量。
        流程：发送 float 格式 → 校验索引无重复/遗漏 → 单位化 → 核对统一维度。
        边界：失败明确上抛，不自动改模型、不把原始服务错误或 API key 写入指标。
        """
        if (
            not texts
            or len(texts) > 16
            or any(not isinstance(text, str) or not text for text in texts)
        ):
            raise ValueError("embedding 输入必须是 1–16 条非空文本")
        options = {"dimensions": self.dimensions} if self.dimensions is not None else {}
        try:
            response = self.client.embeddings.create(
                model=self.model, input=texts, encoding_format="float", **options
            )
        except OpenAIError as error:
            raise RuntimeError(
                f"embedding 请求失败（{type(error).__name__}），未回退文本检索"
            ) from error
        usage = response.usage.total_tokens if response.usage is not None else None
        if usage is not None and (type(usage) is not int or usage < 0):
            raise EmbeddingResponseError("embedding 用量字段无效", known_tokens=None)
        try:
            return self._validate_response(response.data, len(texts), usage)
        except (ValueError, TypeError, AttributeError) as error:
            raise EmbeddingResponseError(str(error), known_tokens=usage) from error

    def _validate_response(self, data, count: int, usage: int | None) -> EmbeddingBatch:
        """核对响应位置/维度；调用层负责保留无效响应的已知计费信息。"""
        vectors: dict[int, list[float]] = {}
        for item in data:
            if type(item.index) is not int or not 0 <= item.index < count or item.index in vectors:
                raise ValueError("embedding 响应 index 重复或越界")
            vectors[item.index] = normalized(item.embedding)
        if len(vectors) != count or len({len(vector) for vector in vectors.values()}) != 1:
            raise ValueError("embedding 响应缺失或维度不一致")
        ordered = [vectors[index] for index in range(count)]
        if self.dimensions is not None and len(ordered[0]) != self.dimensions:
            raise ValueError("embedding 实际维度与配置不一致")
        return EmbeddingBatch(ordered, usage)
