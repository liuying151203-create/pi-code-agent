"""来自 s11：请求模型、生成摘要、校准预算并投影上下文。"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from pi_code_agent import compaction_base as s07
from pi_code_agent import models as previous
from pi_code_agent import serialization as s05
from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.events import AgentEvent, EventDispatchError, EventSink
from pi_code_agent.models import ModelProvider, ModelResponse, ModelUsage
from pi_code_agent.observable_session import (
    CompactionEntry,
    MessageEntry,
    SessionEntry,
    SessionManager,
    _known_usage,
    _model_context_entries,
    build_session_context,
)
from pi_code_agent.serialization import Message, _get

type RequestPurpose = Literal["summary", "assistant"]


def _usage_from_error(error: Exception) -> ModelUsage | None:
    """读取异常或其响应里实际可用的 Token 用量；缺失时返回未知。"""
    raw = _get(error, "usage")
    if raw is None:
        raw = _get(_get(error, "response"), "usage")
    if isinstance(raw, ModelUsage):
        return _known_usage(raw)
    if raw is None:
        return None
    if _get(raw, "input_tokens") is not None and _get(raw, "output_tokens") is not None:
        return _known_usage(
            ModelUsage(
                input_tokens=int(_get(raw, "input_tokens") or 0),
                output_tokens=int(_get(raw, "output_tokens") or 0),
                cache_read_tokens=int(_get(raw, "cache_read_tokens") or 0),
                cache_write_tokens=int(_get(raw, "cache_write_tokens") or 0),
            )
        )
    if _get(raw, "prompt_tokens") is not None:
        return _known_usage(previous._openai_usage(raw))
    return None


@dataclass(frozen=True)
class BlockingModelRequester:
    """发送内部阻塞请求，并报告请求结果和 Token 用量。

    作用：供上下文摘要使用，不产生用户可见文本增量，但参与完整用量统计。
    输入：s10 ModelProvider、超时时间、EventSink、请求用途和统一请求参数。
    输出：Provider 返回的 `ModelResponse`。
    流程：发送 model_request → provider.complete → 发送 model_response；失败时发送错误。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink
    purpose: RequestPurpose = "summary"

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """发送阻塞请求，并在完成时报告用量。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={
                    "model": self.provider.model,
                    "purpose": self.purpose,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
        )
        started_at = perf_counter()
        try:
            response = self.provider.complete(**kwargs)
        except EventDispatchError:
            # s11 修复 B019：观察者故障不是模型失败，不重发错误事件或重复计算用量。
            raise
        except Exception as error:
            duration_ms = (perf_counter() - started_at) * 1000
            # s11 修改：异常若携带 usage，同一次失败调用仍计入用量。
            error_usage = _usage_from_error(error)
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                        "usage": error_usage.to_dict() if error_usage is not None else None,
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：响应完成后一次性记录最终 usage，流式片段不重复计数。
        duration_ms = (perf_counter() - started_at) * 1000
        # s11 修改：把 s10 的全零占位统一解释为未知用量。
        known_usage = _known_usage(response.usage)
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    # s11 修改：Provider 未报告用量时保持 null，而不是伪造零 Token。
                    "usage": known_usage.to_dict() if known_usage is not None else None,
                },
            )
        )
        return response


@dataclass(frozen=True)
class StreamingModelRequester:
    """流式请求正常回答，并在最终响应中报告 Token 用量。

    作用：保持 s10 的 Provider 流式体验，同时让统计器只消费一次最终 usage。
    输入：s10 ModelProvider、超时时间、EventSink、请求用途和统一请求参数。
    输出：Provider 组装完成的 `ModelResponse`。
    流程：发送 model_request → Provider 发送多个 assistant_delta → 取得最终响应
    → 发送一个 model_response；失败时发送 model_error。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink
    purpose: RequestPurpose = "assistant"

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """消费文本流，并在完成时报告一次最终响应。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={
                    "model": self.provider.model,
                    "purpose": self.purpose,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
        )
        started_at = perf_counter()
        try:
            response = self.provider.stream(on_text=self._emit_text, **kwargs)
        except EventDispatchError:
            # s11 修复 B019：增量观察者失败也停止，不能再向坏掉的观察链广播。
            raise
        except Exception as error:
            duration_ms = (perf_counter() - started_at) * 1000
            # s11 修改：流式请求失败也可能返回已消耗的 usage。
            error_usage = _usage_from_error(error)
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                        "usage": error_usage.to_dict() if error_usage is not None else None,
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：以最终完整消息为唯一用量来源，避免按 delta 猜测 Token。
        duration_ms = (perf_counter() - started_at) * 1000
        # s11 修改：Provider 缺失 usage 时，不把 s10 全零占位当成实际消耗。
        known_usage = _known_usage(response.usage)
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    # s11 修改：成功结束但没有 usage 时仍保留未知状态。
                    "usage": known_usage.to_dict() if known_usage is not None else None,
                },
            )
        )
        return response

    def _emit_text(self, text: str) -> None:
        """把 Provider 文本片段转换为流式展示事件。"""
        self.emit(AgentEvent(type="assistant_delta", data={"text": text}))


@dataclass(frozen=True)
class SummaryResult:
    """把摘要正文与生成它的用量一起交给压缩器。

    输入：摘要文字、模型和可选 usage；输出：摘要器与压缩器间的结果对象。
    流程：摘要器提取有效正文 → 压缩器复核长度 → 随 CompactionEntry 保存。
    边界：不发起请求、不落盘，失败调用由已有失败记录通道处理。
    """

    text: str | None
    model: str | None
    usage: ModelUsage | None


def _summary_response_details(
    response: ModelResponse, summary: str, max_tokens: int, request_options: dict[str, Any]
) -> dict[str, Any]:
    """统计 Provider 转换后的内容块；未知块只记录字段名和字符串长度。"""
    content = response.content
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    content_types: list[str] = []
    text_chars = 0
    thinking_chars = 0
    other_blocks: list[dict[str, Any]] = []
    for block in blocks:
        block_type = str(_get(block, "type") or "unknown")
        content_types.append(block_type)
        if block_type == "text":
            text_chars += len(str(_get(block, "text") or ""))
        elif block_type in {"thinking", "reasoning"}:
            thinking = (
                _get(block, "thinking")
                or _get(block, "reasoning_content")
                or _get(block, "text")
                or ""
            )
            thinking_chars += len(str(thinking))
        else:
            fields = block if isinstance(block, dict) else {}
            other_blocks.append(
                {
                    "type": block_type,
                    "fields": sorted(fields),
                    "string_chars": sum(
                        len(value) for value in fields.values() if isinstance(value, str)
                    ),
                }
            )
    # s11 新增：明确诊断发生在 Provider 转换之后，空内容不能证明 SDK 原始响应为空。
    usage = _known_usage(response.usage)
    return {
        "response_stage": "normalized",
        "stop_reason": response.stop_reason,
        "max_tokens": max_tokens,
        "requested_thinking": _get(request_options.get("thinking"), "type"),
        "usage": usage.to_dict() if usage is not None else None,
        "content_types": content_types,
        "text_chars": text_chars,
        "thinking_chars": thinking_chars,
        "extracted_text_chars": len(summary),
        "other_blocks": other_blocks,
    }


def show_summary_diagnostic(event: AgentEvent) -> None:
    """显示空摘要的结构证据，不显示正文或思考原文。

    输入：现有 EventDispatcher 广播的事件；只处理含诊断的 summary_empty。
    输出：一行终端诊断，其他事件不产生输出。
    流程：读取结构元数据 → 显示停止原因、请求额度与内容长度；完整元数据由 Trace 保存。
    """
    if event.type != "summary_empty" or "response_details" not in event.data:
        return
    details = event.data["response_details"]
    print(
        f"摘要诊断  停止={details['stop_reason']}；额度={details['max_tokens']} tokens；"
        f"请求思考={details['requested_thinking'] or '接口默认'}；"
        f"内容块={','.join(details['content_types']) or '无'}；"
        f"正文={details['text_chars']} 字符；思考={details['thinking_chars']} 字符；"
        f"提取正文={details['extracted_text_chars']} 字符。",
        flush=True,
    )


@dataclass(frozen=True)
class ContextSummarizer:
    """生成摘要并记录请求结果；失败时停止，保留原始会话证据。

    输入：待总结消息、旧摘要、请求器、会话、事件出口与摘要专用参数。
    输出：成功时返回 s11 SummaryResult；无文本时发出诊断，抛出 RuntimeError 给入口。
    流程：组装摘要请求 → 提取正文 → 成功返回，或保存失败用量并报告结构诊断。
    边界：空摘要仍采用 s07 的停止策略，不写 CompactionEntry；诊断不记录原文。
    """

    create_message: Callable[..., ModelResponse]
    max_tokens: int
    session: SessionManager
    emit: EventSink
    # 来自 s07：保持；参数只用于摘要，由 s10 Provider 层按接口选择。
    request_options: dict[str, Any] | None = None

    def __call__(
        self,
        messages_to_summarize: Sequence[Message],
        previous_summary: str | None,
    ) -> SummaryResult:
        """使用待总结消息和旧摘要生成 SummaryResult，并记录失败请求的用量。

        输入：尚未摘要的消息与可选旧摘要；请求依赖已在构造时注入。
        输出：含正文和成功请求用量的 SummaryResult；无正文时抛出 RuntimeError。
        流程：组装请求 → 提取正文 → 成功返回；失败先保存用量，再发出诊断并停止。
        本方法不写压缩记录；只有压缩器拿到有效摘要后才更新会话。
        """
        # 来自 s07：保持；沿用消息序列化与专用摘要提示词。
        transcript = s07.serialize_context_for_summary(messages_to_summarize)
        previous_section = (
            f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            if previous_summary
            else ""
        )
        prompt = (
            f"{previous_section}<unsummarized-messages>\n{transcript}\n"
            "</unsummarized-messages>\n\n请生成更新后的完整摘要。"
        )
        # 来自 s07：保持；不足 1024 时仅重试一次，不自动放大已配置的高额度。
        token_limits = [self.max_tokens]
        # 来自 s07：保持；不向正常回答请求传递摘要的专用选项。
        options = (
            s07.SUMMARY_REQUEST_OPTIONS if self.request_options is None else self.request_options
        )
        if self.max_tokens < 1024:
            token_limits.append(1024)
        for token_limit in token_limits:
            response = self.create_message(
                system=s07.COMPACTION_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=token_limit,
                **options,
            )
            summary = s05._text_from_content(response.content).strip()
            # s11 修改：错误或中断响应即使带部分正文，也不能作为可信摘要。
            if summary and response.stop_reason not in {"error", "aborted"}:
                return SummaryResult(summary, response.model, _known_usage(response.usage))

            # s11 新增：每次未形成摘要的请求只持久化一次用量。
            self.session.append_model_error(response.model, "summary", _known_usage(response.usage))
            if response.stop_reason not in {"error", "aborted"}:
                # s11 新增：扩充原有事件，Trace 自动保存诊断，统计器仍只计一次失败。
                details = _summary_response_details(response, summary, token_limit, options)
                self.emit(
                    AgentEvent(
                        type="summary_empty",
                        data={"model": response.model, "response_details": details},
                    )
                )
        # s11 修复：仍在压缩落盘之前停止；诊断不将 thinking 当作摘要，也不改变预算。
        raise RuntimeError(
            "摘要模型未返回正文，已停止本次任务，原始会话仍保留；"
            "不会用缺少代码证据的回退摘要继续重复调用工具。"
        )


def estimate_session_context_tokens(entries: Sequence[SessionEntry]) -> int:
    """估算当前投影占用的 Token，优先使用压缩点之后的有效回答用量。

    输入：按时间排列的会话 Entry。
    输出：最近有效回答的上下文用量加后续新增消息估算；没有有效 usage 时估算完整投影。
    流程：逆序寻找回答 → 排除失败和未知用量 → 加上后续消息；遇到压缩点停止回溯。
    边界：旧压缩前的 usage 对新投影已失效；摘要请求用量不是正常回答的上下文大小。
    """
    # s11 修复 B018：失败回答及旧失败尾部不参与新增消息估算。
    entries = _model_context_entries(entries)
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if isinstance(entry, CompactionEntry):
            break
        if (
            isinstance(entry, MessageEntry)
            and entry.message.get("role") == "assistant"
            and entry.stop_reason not in {"error", "aborted"}
            and _known_usage(entry.usage) is not None
        ):
            later_messages = [
                later.message for later in entries[index + 1 :] if isinstance(later, MessageEntry)
            ]
            return entry.usage.total_tokens + s07.estimate_context_tokens(later_messages)
    return s07.estimate_context_tokens(build_session_context(entries))


class ContextCompactor:
    """执行 s07 的上下文压缩，并把摘要请求的用量写入压缩记录。

    输入：会话、压缩策略和返回 SummaryResult 的摘要器。
    输出：未触发压缩时返回 None；触发后返回压缩前后大小等结果。
    流程：准备计划 → 调用摘要器 → 拒绝空摘要 → 限制长度 → 保存摘要与用量。
    """

    def __init__(
        self,
        session: SessionManager,
        policy: CompactionPolicy,
        summarize: ContextSummarizer,
    ) -> None:
        self.session = session
        self.policy = policy
        self.summarize = summarize

    def compact_if_needed(self) -> s07.CompactionOutcome | None:
        """校准上下文大小，再执行压缩并持久化成功摘要的用量。

        输入：实例中的会话、Token 策略与摘要器。
        输出：不需要压缩时返回 None；成功返回大小统计；摘要失败或大小校验失败时报错。
        流程：读取 entries → 用有效 usage 估算 → s07 准备计划 → 摘要 → 复核 → 保存。
        未形成压缩记录的失败请求仍保留用量，不用会话累计消费判断当前上下文。
        """
        # s11 修改：实际上下文用量与会话累计统计分开，准备阶段不依赖 Provider 类型。
        entries = self.session.load_entries()
        # s11 修复 B018：与请求投影使用同一有效记录，压缩不能重新保留失败回答。
        model_entries = _model_context_entries(entries)
        context_tokens = estimate_session_context_tokens(entries)
        plan = s07.prepare_compaction(model_entries, self.policy, context_tokens=context_tokens)
        if plan is None:
            return None

        summary_result = self.summarize(plan.messages_to_summarize, plan.previous_summary)
        if summary_result.text is None:
            # 来自 s07：保持；空摘要不能覆盖上下文，保留原始会话并停止。
            raise RuntimeError("摘要模型未返回正文，已停止本次任务，原始会话仍保留。")
        summary = summary_result.text
        summary_kind = "model"

        summary = s07._fit_summary_to_budget(
            summary,
            plan.retained_tail,
            self.policy.max_context_chars,
            self.session.path,
        )
        after_chars = s07.estimate_context_chars(s07._summary_context(summary, plan.retained_tail))
        if after_chars > self.policy.max_context_chars:
            # s11 修正：压缩校验失败时没有 CompactionEntry，已发生的摘要用量单独落盘。
            if summary_result.model is not None:
                self.session.append_model_error(
                    summary_result.model,
                    "summary",
                    summary_result.usage,
                )
            raise RuntimeError("压缩后上下文仍超过 Token 预算的近似字符上限")

        # s11 修改：参考 Pi，成功摘要的用量随 CompactionEntry 保存；空响应已单独落盘。
        self.session.append_compaction(
            summary,
            plan.retained_tail,
            summary_model=summary_result.model,
            summary_usage=summary_result.usage,
        )
        return s07.CompactionOutcome(
            summary_kind=summary_kind,
            before_chars=plan.before_chars,
            after_chars=after_chars,
            summarized_messages=len(plan.messages_to_summarize),
            retained_messages=len(plan.retained_tail),
        )


@dataclass(frozen=True)
class CompactedContextRequester:
    """请求模型前按需压缩上下文，并报告压缩结果。

    作用：保持 s10 的上下文边界，把最新活跃上下文交给流式请求器。
    输入：底层请求函数、当前会话、压缩器、EventSink 和请求参数。
    输出：底层请求器返回的最终完整响应。
    流程：尝试压缩 → 报告压缩 → 重建上下文 → 发起正常回答请求。
    """

    create_message: Callable[..., Any]
    session: SessionManager
    compactor: ContextCompactor
    emit: EventSink

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """按需压缩，并使用最新会话上下文请求正常回答。"""
        outcome = self.compactor.compact_if_needed()
        if outcome is not None:
            self.emit(
                AgentEvent(
                    type="compaction",
                    data={
                        "summary_kind": outcome.summary_kind,
                        "before_chars": outcome.before_chars,
                        "after_chars": outcome.after_chars,
                        "summarized_messages": outcome.summarized_messages,
                        "retained_messages": outcome.retained_messages,
                    },
                )
            )
        kwargs["messages"] = self.session.build_context()
        return self.create_message(**kwargs)


def _tool_result_is_error(output: str) -> bool:
    """根据统一工具结果前缀判断本次执行是否失败。"""
    return output.startswith(("Error:", "Permission denied:", "Hook error:"))


def model_round_limit_from_env() -> int | None:
    """读取 AGENT_MAX_ROUNDS，返回可选回答请求上限。

    输入：环境变量，未填写时使用 0。
    输出：正整数表示限制，0 转为 None 表示默认继续；负数或非整数报错。
    流程：解析配置 → 校验范围 → 交给 agent_loop，不影响摘要请求额度。
    """
    value = int(os.getenv("AGENT_MAX_ROUNDS", "0"))
    if value < 0:
        raise ValueError("AGENT_MAX_ROUNDS 不能小于 0")
    return value or None
