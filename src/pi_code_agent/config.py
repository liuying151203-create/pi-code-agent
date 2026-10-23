"""s17：显式配置、环境快照和校验；不创建模型客户端或运行记录。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from pi_code_agent.compaction_base import CompactionPolicy


# ===== s17 新增：当前运行会用到的值校验 =====
# s17 新增：解析失败只报告字段与约束，不把可能误填的凭证写入异常。
def _integer(name: str, value: object, minimum: int) -> None:
    """检查整数类型和下界，拒绝布尔值冒充整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")


def _int_env(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    """读取整数字段；错误值不进入异常信息和异常链。"""
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None
    _integer(name, value, minimum)
    return value


# s17 新增：客户端超时和事件标注共用数值约束，但不将二者误当作任务取消。
def validate_timeout(timeout_seconds: float) -> None:
    """校验超时额度，供配置和显式 Agent 参数在创建资源前调用。

    输入：秒数；输出：None，非法值抛出 ValueError。
    流程：检查数值类型 → 拒绝非有限值及非正值；不操作 Provider。
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("MODEL_TIMEOUT_SECONDS / timeout_seconds 必须是有限正数")


# ===== s17 新增：明确模型与运行选项的职责边界 =====
# s17 新增：参考 Pi 的显式设置注入，只保留当前请求所需字段，不加入持久化设置管理器。
@dataclass(frozen=True)
class ModelConfig:
    """保存内置 Provider 的模型、认证和请求参数。

    输入：模型名、Provider、认证、可选地址、请求超时和 Provider 重试次数。
    输出：通过校验的不可变配置，凭证不参与 repr。
    流程：构造时校验 → create_model_provider 显式创建客户端。
    边界：不读环境、不创建客户端；高级认证可由调用方自建 Provider。
    """

    model: str
    provider: str = "anthropic"
    api_key: str | None = field(default=None, repr=False)
    auth_token: str | None = field(default=None, repr=False)
    base_url: str | None = None
    timeout_seconds: float = 60
    max_retries: int = 0

    def __post_init__(self) -> None:
        """在客户端创建之前校验字段，错误信息不包含输入值。"""
        if not isinstance(self.provider, str) or self.provider not in {
            "anthropic",
            "openai-compatible",
        }:
            raise ValueError("MODEL_PROVIDER / provider 仅支持 anthropic 或 openai-compatible")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("MODEL_ID / model 必须是非空字符串")
        for name, value in (("api_key", self.api_key), ("auth_token", self.auth_token)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 必须是非空字符串或 None")
        if self.api_key is not None and self.auth_token is not None:
            raise ValueError("api_key 与 auth_token 只能提供一个")
        if self.provider == "anthropic" and not (self.api_key or self.auth_token):
            raise ValueError("Anthropic 需要 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN")
        if self.provider == "openai-compatible" and (not self.api_key or self.auth_token):
            raise ValueError("openai-compatible 需要 MODEL_API_KEY / api_key")
        if self.base_url is not None:
            if not isinstance(self.base_url, str):
                raise ValueError("base_url 必须是无用户凭证的 HTTP(S) 地址")
            try:
                url = urlsplit(self.base_url)
                valid = url.scheme in {"http", "https"} and bool(url.hostname)
                valid = valid and url.username is None and url.password is None
                _ = url.port
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("base_url 必须是无用户凭证的 HTTP(S) 地址") from None
        validate_timeout(self.timeout_seconds)
        _integer("MODEL_MAX_RETRIES / max_retries", self.max_retries, 0)


# s17 新增：运行选项与模型认证分离，自定义 Provider 无需构造虚假的模型配置。
@dataclass(frozen=True)
class RunConfig:
    """保存循环和压缩器当前会使用的运行选项。

    输入：回答轮数、输出额度、可选压缩策略和摘要额度。
    输出：通过校验的不可变配置；入口逐项传给现有 create_agent。
    流程：校验数值及策略 → 创建 Agent → 参数进入循环或请求链。
    边界：不读环境、不启用隐藏的压缩或重试，不增加第二套 Agent 工厂接口。
    """

    max_rounds: int | None = None
    max_tokens: int = 8000
    compaction_policy: CompactionPolicy | None = None
    summary_max_tokens: int = 4096

    def __post_init__(self) -> None:
        """保证直接 SDK 调用与环境解析都在打开会话前发现非法额度。"""
        if self.max_rounds is not None:
            _integer("AGENT_MAX_ROUNDS / max_rounds", self.max_rounds, 1)
        _integer("MODEL_MAX_TOKENS / max_tokens", self.max_tokens, 1)
        _integer("summary_max_tokens", self.summary_max_tokens, 1)
        if self.compaction_policy is not None:
            if not isinstance(self.compaction_policy, CompactionPolicy):
                raise ValueError("compaction_policy 必须是 CompactionPolicy 或 None")
            for name in ("context_window_tokens", "reserve_tokens", "keep_recent_tokens"):
                _integer(name, getattr(self.compaction_policy, name), 1)


# ===== s17 新增：明确来源的环境快照，不修改 os.environ =====
def _model_aliases(values: Mapping[str, str]) -> dict[str, str]:
    """在单个来源内归一兼容别名，随后才能按来源优先级合并。"""
    result = dict(values)
    for name, alias in (("MODEL_API_KEY", "OPENAI_API_KEY"), ("MODEL_BASE_URL", "OPENAI_BASE_URL")):
        if name in values or alias in values:
            result[name] = values.get(name) or values.get(alias) or ""
        result.pop(alias, None)
    return result


# s17 新增：与 lcc 的 load_dotenv 全局写入不同，本章让入口持有可检查的配置快照。
def load_environment(env_file: Path, *, environ: Mapping[str, str]) -> dict[str, str]:
    """读取明确文件并合并进程环境，返回无全局副作用的快照。

    输入：明确的 .env 路径和进程环境映射；输出：环境覆盖文件后的新字典。
    流程：文件存在才读取 → 每层归一模型别名 → 合并，进程环境优先。
    边界：不向上寻找文件、不从目标 workspace 偷读配置、不执行变量插值。
    """
    values = {}
    if env_file.exists():
        with env_file.open(encoding="utf-8-sig") as stream:
            values = {
                name: value if value is not None else ""
                for name, value in dotenv_values(stream=stream, interpolate=False).items()
            }
    merged = _model_aliases(values)
    merged.update(_model_aliases(environ))
    return merged


def model_config_from_env(env: Mapping[str, str]) -> ModelConfig:
    """将调用方提供的环境快照转为内置模型配置。

    输入：字符串映射；输出：ModelConfig；不创建客户端、不读取额外环境。
    流程：归一别名 → 选择接口与凭证 → 解析数值 → 由 ModelConfig 校验。
    """
    values = _model_aliases(env)
    provider = values.get("MODEL_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        api_key = values.get("ANTHROPIC_API_KEY", "").strip() or None
        auth_token = None if api_key else values.get("ANTHROPIC_AUTH_TOKEN", "").strip() or None
        base_url = values.get("ANTHROPIC_BASE_URL", "").strip() or None
    else:
        api_key = values.get("MODEL_API_KEY", "").strip() or None
        auth_token = None
        base_url = values.get("MODEL_BASE_URL", "").strip() or None
    try:
        timeout_seconds = float(values.get("MODEL_TIMEOUT_SECONDS", "60"))
    except (TypeError, ValueError):
        raise ValueError("MODEL_TIMEOUT_SECONDS 必须是有限正数") from None
    return ModelConfig(
        model=values.get("MODEL_ID", "").strip(),
        provider=provider,
        api_key=api_key,
        auth_token=auth_token,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=_int_env(values, "MODEL_MAX_RETRIES", 0, 0),
    )


def run_config_from_env(env: Mapping[str, str], *, enable_compaction: bool = False) -> RunConfig:
    """将环境快照转为当前运行额度，压缩仅在调用方明确启用时解析。

    输入：字符串映射及是否启用压缩；输出：RunConfig。
    流程：解析回答额度 → 可选构造已有 CompactionPolicy → 校验并返回。
    边界：环境的轮数 0 转为 None；未启用压缩时不要求窗口或读取摘要选项。
    """
    max_rounds = _int_env(env, "AGENT_MAX_ROUNDS", 0, 0) or None
    max_tokens = _int_env(env, "MODEL_MAX_TOKENS", 8000, 1)
    policy = None
    summary_max_tokens = 4096
    if enable_compaction:
        policy = CompactionPolicy(
            context_window_tokens=_int_env(env, "MODEL_CONTEXT_WINDOW_TOKENS", 0, 1),
            reserve_tokens=_int_env(env, "SESSION_COMPACTION_RESERVE_TOKENS", 16384, 1),
            keep_recent_tokens=_int_env(env, "SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000, 1),
        )
        summary_max_tokens = _int_env(
            env, "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", int(policy.reserve_tokens * 0.8), 1
        )
    return RunConfig(
        max_rounds=max_rounds,
        max_tokens=max_tokens,
        compaction_policy=policy,
        summary_max_tokens=summary_max_tokens,
    )
