# ===== s07 快照 =====
# 来自 s06：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s03：显式模型配置和 CLI 环境快照；SDK 构造不会隐式读取 .env。"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values


# 来自 s03：保持；超时、重试和额度经过统一校验，真正传到请求组件。
@dataclass(frozen=True)
class ModelConfig:
    """保存一次运行的模型配置，不创建客户端，不加载文件。

    输入：协议、模型、密钥、地址及请求参数；输出：已校验配置快照。
    流程：SDK 显式构造或 CLI 读取环境 → 校验 → Provider 消费。
    边界：模型名称由用户指定，不猜测窗口、模型能力或不同服务的兼容程度。
    """

    provider: Literal["anthropic", "openai-compatible"]
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    timeout_seconds: float = 60
    max_retries: int = 0
    max_tokens: int = 8000
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    # 来自 s05：保持；兼容服务可显式关闭不支持的 stream_options，不按模型名猜测。
    stream_include_usage: bool = True

    def __post_init__(self) -> None:
        """在创建客户端和发请求前拒绝无效配置。"""
        if type(self.stream_include_usage) is not bool:
            raise ValueError("stream_include_usage 必须是布尔值")
        if self.provider not in {"anthropic", "openai-compatible"}:
            raise ValueError("MODEL_PROVIDER 只支持 anthropic 或 openai-compatible")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("MODEL_ID 不能为空")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("模型 API Key 不能为空")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("MODEL_TIMEOUT_SECONDS 必须是有限正数")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("MODEL_MAX_RETRIES 必须是非负整数")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("MODEL_MAX_TOKENS 必须是正整数")
        if self.token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("MODEL_TOKEN_PARAMETER 必须是 max_tokens 或 max_completion_tokens")
        if self.provider == "anthropic" and self.token_parameter != "max_tokens":
            raise ValueError("Anthropic 使用 max_tokens 额度字段")
        if self.base_url is not None:
            if not isinstance(self.base_url, str):
                raise ValueError("模型地址必须是 HTTP(S) URL 字符串")
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("模型地址必须是完整的 HTTP(S) URL")


def load_model_config(env_file: Path, *, environ: Mapping[str, str] | None = None) -> ModelConfig:
    """为 CLI 加载文件与环境，进程环境优先，不修改 os.environ。

    输入：明确的 .env 路径和可选进程环境映射；输出：ModelConfig。
    流程：解析无插值文件 → 合并环境 → 读取协议对应密钥 → 构造并校验。
    """
    values = {
        key: value
        for key, value in dotenv_values(env_file, interpolate=False).items()
        if value is not None
    }
    values.update(os.environ if environ is None else environ)
    provider = values.get("MODEL_PROVIDER", "anthropic")
    key_name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "MODEL_API_KEY"
    url_name = "ANTHROPIC_BASE_URL" if provider == "anthropic" else "MODEL_BASE_URL"
    # 来自 s05：保持；只接受明确的布尔配置，避免字符串 false 被当作真值。
    stream_usage = values.get("MODEL_STREAM_INCLUDE_USAGE", "true").lower()
    if stream_usage not in {"true", "false"}:
        raise ValueError("MODEL_STREAM_INCLUDE_USAGE 必须是 true 或 false")
    return ModelConfig(
        provider=provider,
        model=values.get("MODEL_ID", ""),
        api_key=values.get(key_name, ""),
        base_url=values.get(url_name) or None,
        timeout_seconds=float(values.get("MODEL_TIMEOUT_SECONDS", "60")),
        max_retries=int(values.get("MODEL_MAX_RETRIES", "0")),
        max_tokens=int(values.get("MODEL_MAX_TOKENS", "8000")),
        token_parameter=values.get("MODEL_TOKEN_PARAMETER", "max_tokens"),
        stream_include_usage=stream_usage == "true",
    )
