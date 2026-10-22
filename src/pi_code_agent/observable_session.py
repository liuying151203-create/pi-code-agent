"""来自 s11：保存有效历史、失败用量，并从完整日志恢复会话统计。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pi_code_agent import compaction_base as s07
from pi_code_agent.models import ModelResponse, ModelUsage
from pi_code_agent.serialization import Message, _get

if TYPE_CHECKING:
    from pi_code_agent.events import AgentEvent


def _usage_from_record(value: object) -> ModelUsage | None:
    """把可选的 JSON 用量还原为统一模型用量。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("usage 必须是对象或 null")
    names = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    counts = {}
    for name in names:
        # s11 修正：部分字段缺失不能被补成零，否则历史统计会低估。
        if name not in value:
            raise ValueError(f"usage 缺少 {name}")
        count = value[name]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"usage.{name} 必须是非负整数")
        counts[name] = count
    return ModelUsage(**counts)


def _known_usage(usage: ModelUsage | None) -> ModelUsage | None:
    """把 s10 的全零占位视为未报告用量。

    s10 的 `ModelResponse` 固定携带 `ModelUsage`，Provider 缺少 usage 时会生成全零对象。
    实际模型调用至少消耗输入或输出 Token，因此 s11 将全零解释为未知。
    """
    if usage is None or usage.total_tokens == 0:
        return None
    return usage


@dataclass(frozen=True)
class MessageEntry(s07.MessageEntry):
    """保存消息事实及产生 assistant 的模型元数据。

    输入：模型消息、停止原因和可选用量；输出：可序列化的消息 Entry。
    流程：循环保存回答 → 日志恢复元数据 → 投影判断回答有效性，统计读取用量。
    边界：失败回答仍留在日志中，不等于它可以继续发送给模型。
    """

    model: str | None = None
    stop_reason: str | None = None
    usage: ModelUsage | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> MessageEntry:
        """读取本章或旧版 s11 的消息记录。"""
        base = s07.MessageEntry.from_record(record)
        return cls(
            message=base.message,
            timestamp=base.timestamp,
            model=record.get("model"),
            stop_reason=record.get("stop_reason"),
            usage=_usage_from_record(record.get("usage")),
        )

    def to_record(self) -> dict[str, Any]:
        """将消息及其可选模型元数据写入同一条记录。"""
        record = super().to_record()
        if self.model is not None:
            record.update(
                model=self.model,
                stop_reason=self.stop_reason,
                usage=self.usage.to_dict() if self.usage is not None else None,
            )
        return record


@dataclass(frozen=True)
class CompactionEntry(s07.CompactionEntry):
    """保存压缩投影边界及生成摘要的用量。

    输入：摘要、保留尾部、摘要模型和用量；输出：compaction JSONL 记录。
    流程：压缩器保存 → 模型投影使用摘要和尾部 → 会话统计累计摘要用量。
    边界：不删除旧消息，尾部只含模型消息，不重复记录它们原来的用量。
    """

    summary_model: str | None = None
    summary_usage: ModelUsage | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CompactionEntry:
        """读取本章或旧版 s11 的压缩记录。"""
        base = s07.CompactionEntry.from_record(record)
        return cls(
            summary=base.summary,
            retained_tail=base.retained_tail,
            timestamp=base.timestamp,
            summary_model=record.get("summary_model"),
            summary_usage=_usage_from_record(record.get("summary_usage")),
        )

    def to_record(self) -> dict[str, Any]:
        """把摘要请求的用量附在压缩记录上。"""
        record = super().to_record()
        record["summary_model"] = self.summary_model
        record["summary_usage"] = (
            self.summary_usage.to_dict() if self.summary_usage is not None else None
        )
        return record


@dataclass(frozen=True)
class ModelErrorEntry:
    """保存没有形成模型消息的一次失败调用及其可选用量。"""

    model: str
    purpose: str
    usage: ModelUsage | None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ModelErrorEntry:
        """读取失败调用记录。"""
        return cls(
            model=str(record["model"]),
            purpose=str(record["purpose"]),
            usage=_usage_from_record(record.get("usage")),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """把失败调用转换为 JSONL 记录。"""
        return {
            "type": "model_error",
            "model": self.model,
            "purpose": self.purpose,
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "timestamp": self.timestamp,
        }


type SessionEntry = MessageEntry | CompactionEntry | ModelErrorEntry


type AssistantSaver = Callable[[Message, ModelResponse], None]


def _model_context_entries(entries: Sequence[SessionEntry]) -> list[SessionEntry]:
    """过滤失败回答，并清理旧压缩尾部中的同一失败消息。

    输入：完整会话 Entry；输出：仅供投影和压缩使用的记录列表，不写回日志。
    流程：读取回答停止原因 → 记录原投影中的有效标记 → 压缩尾部匹配原投影后过滤。
    边界：尾部不保存 stop_reason，因此从它对应的原消息继承标记；不按正文去重，
    避免把文字相同的正常回答误删。手工写入、无法匹配来源的尾部保持原样。
    """
    model_entries: list[SessionEntry] = []
    source_messages: list[Message] = []
    source_failed: list[bool] = []
    for entry in entries:
        if isinstance(entry, MessageEntry):
            failed = entry.stop_reason in {"error", "aborted"}
            source_messages.append(entry.message)
            source_failed.append(failed)
            if not failed:
                model_entries.append(entry)
        elif isinstance(entry, CompactionEntry):
            tail = list(entry.retained_tail)
            # s11 修复 B018：用位置恢复旧尾部的来源，正文相同也不混淆成功和失败。
            tail_failed = [False] * len(tail)
            if tail and source_messages[-len(tail) :] == tail:
                tail_failed = source_failed[-len(tail) :]
            valid_tail = tuple(message for message, failed in zip(tail, tail_failed) if not failed)
            model_entries.append(replace(entry, retained_tail=valid_tail))
            # s11 修复 B018：原投影及标记一起推进，连续旧压缩仍能追踪失败尾部。
            source_messages = [s07._summary_message(entry), *tail]
            source_failed = [False, *tail_failed]
        else:
            model_entries.append(entry)
    return model_entries


def _complete_tool_result_pairs(messages: Sequence[Message]) -> list[Message]:
    """为缺失工具结果补充明确的错误反馈，保持历史协议配对。

    输入：投影后的有效消息；输出：新的、调用与结果配对的模型消息列表。
    流程：记住 assistant 调用 → 补齐下一条结果中的缺失项，或在新对话前插入错误结果。
    边界：结果缺失不能证明工具没执行；提示不要自动重放，不虚构成功或 Todo 快照。
    补齐只发生在请求上下文，不增加持久化工具执行或用量记录。
    """
    context: list[Message] = []
    pending_results: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        result_message = (
            message.get("role") == "user"
            and isinstance(content, list)
            and any(_get(block, "type") == "tool_result" for block in content)
        )
        if pending_results:
            present_ids = (
                {
                    _get(block, "tool_use_id")
                    for block in content
                    if _get(block, "type") == "tool_result"
                }
                if result_message
                else set()
            )
            missing = [
                result for result in pending_results if result["tool_use_id"] not in present_ids
            ]
            if missing:
                # s11 修复 B019：有部分结果时只补缺失项；没有结果时先结束未完成调用。
                if result_message:
                    message = {**message, "content": [*content, *missing]}
                else:
                    context.append({"role": "user", "content": missing})
            pending_results = []
        context.append(message)
        if message.get("role") == "assistant" and isinstance(content, list):
            pending_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": "Error: tool result was not saved; do not replay this call automatically",
                }
                for block in content
                if _get(block, "type") == "tool_use"
            ]
    # s11 修复 B019：日志恰好结束在调用处时，也补齐只用于模型输入的错误结果。
    if pending_results:
        context.append({"role": "user", "content": pending_results})
    return context


def build_session_context(entries: Sequence[SessionEntry]) -> list[Message]:
    """从完整日志生成可继续请求模型的上下文。

    输入：消息、压缩和失败调用 Entry；输出：不含失败回答且工具配对完整的消息。
    流程：排除失败回答 → 复用 s07 最新压缩点投影 → 补齐缺失结果；日志和统计不变。
    """
    # s11 修复 B018/B019：两个独立边界显式排列，不能把完整事实直接当模型输入。
    model_entries = _model_context_entries(entries)
    messages = s07.build_session_context(model_entries)
    return _complete_tool_result_pairs(messages)


def entry_from_record(record: object) -> SessionEntry:
    """根据类型恢复本章实际使用的三种会话 Entry。"""
    if not isinstance(record, dict):
        raise TypeError("会话记录必须是对象")
    if record.get("type") == "message":
        return MessageEntry.from_record(record)
    if record.get("type") == "compaction":
        return CompactionEntry.from_record(record)
    if record.get("type") == "model_error":
        return ModelErrorEntry.from_record(record)
    raise ValueError("会话记录类型必须是 message、compaction 或 model_error")


class JsonlSessionStore(s07.JsonlSessionStore):
    """读取消息、压缩和失败调用，追加写入复用 s07。

    输入：会话路径；输出：带模型元数据的完整 Entry 列表。
    流程：逐行解析 → 根据类型恢复对象 → 将格式错误连同行号报告。
    边界：只处理日志 I/O，有效上下文的选择交给 SessionManager。
    """

    def read_all(self) -> list[SessionEntry]:
        """逐行读取消息、压缩和失败调用记录。"""
        if not self.path.exists():
            return []
        entries: list[SessionEntry] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                entries.append(entry_from_record(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"无效会话记录，第 {line_number} 行：{error}") from error
        return entries


class SessionManager(s07.SessionManager):
    """管理 s11 会话的完整记录，并向运行时提供不含统计元数据的模型上下文。

    输入：绑定会话文件的存储对象，以及按调用结果产生的消息、摘要或异常。
    输出：可重放的会话 Entry、模型上下文和会话累计用量。
    流程：按类型追加记录；加载时还原元数据；构建上下文时沿用 s07 投影。
    """

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开一个 s11 会话文件。"""
        return cls(JsonlSessionStore(path))

    # s11 修改：投影排除失败回答并修复调用配对，原日志和累计用量仍完整保留。
    def build_context(self) -> list[Message]:
        """读取完整日志，生成有效模型历史，不执行或重新保存工具。

        输入：本会话的全部 Entry；输出：有效对话、最新摘要和保留尾部。
        流程：load_entries → build_session_context；恢复历史与正常请求使用同一规则。
        """
        entries = self.load_entries()
        return build_session_context(entries)

    def append_assistant(self, message: Message, response: ModelResponse) -> None:
        """把 assistant 内容与生成它的模型用量保存为同一条记录。"""
        base = MessageEntry.from_message(message)
        self.store.append(
            MessageEntry(
                message=base.message,
                model=response.model,
                stop_reason=response.stop_reason,
                usage=_known_usage(response.usage),
            )
        )

    def append_compaction(
        self,
        summary: str,
        retained_tail: Sequence[Message],
        summary_model: str | None = None,
        summary_usage: ModelUsage | None = None,
    ) -> None:
        """把摘要、保留尾部和生成摘要的那次用量一起保存。"""
        base = CompactionEntry.create(summary, retained_tail)
        self.store.append(
            CompactionEntry(
                summary=base.summary,
                retained_tail=base.retained_tail,
                summary_model=summary_model,
                summary_usage=_known_usage(summary_usage),
            )
        )

    def append_model_error(self, model: str, purpose: str, usage: ModelUsage | None) -> None:
        """保存没有形成消息的失败模型调用。"""
        self.store.append(ModelErrorEntry(model=model, purpose=purpose, usage=usage))

    def session_stats(self) -> SessionStats:
        """从完整会话历史重算累计用量，包含已压缩的旧消息。"""
        return SessionStats.from_entries(self.load_entries())


@dataclass
class SessionStats:
    """从持久化会话记录计算累计模型调用和已知 Token 用量。

    输入：全部消息、压缩和失败调用 Entry。
    输出：请求总数、失败数、未知用量次数及已知 Token 总数。
    流程：逐条读取对应调用的 usage；压缩不删除旧记录，恢复后可重新汇总。
    """

    model_requests: int = 0
    summary_requests: int = 0
    failed_requests: int = 0
    tool_calls: int = 0
    unknown_usage_requests: int = 0
    legacy_entries: int = 0
    known_tokens: int = 0

    @classmethod
    def from_entries(cls, entries: Sequence[SessionEntry]) -> SessionStats:
        """按记录归属累计回答、摘要和无消息失败调用。"""
        stats = cls()
        # s11 新增：一条调用只在其归属的 Entry 计数一次。
        for entry in entries:
            if isinstance(entry, MessageEntry) and entry.message["role"] == "assistant":
                # s11 新增：参考 Pi，从已保存的 assistant 内容块重算工具调用数。
                content = entry.message["content"]
                if isinstance(content, list):
                    stats.tool_calls += sum(_get(block, "type") == "tool_use" for block in content)
            if isinstance(entry, ModelErrorEntry):
                stats.model_requests += 1
                stats.failed_requests += 1
                if entry.purpose == "summary":
                    stats.summary_requests += 1
                stats.add_usage(entry.usage)
            elif isinstance(entry, CompactionEntry):
                if entry.summary_model is not None:
                    stats.model_requests += 1
                    stats.summary_requests += 1
                    stats.add_usage(entry.summary_usage)
            elif isinstance(entry, MessageEntry) and entry.model is not None:
                stats.model_requests += 1
                if entry.stop_reason in {"error", "aborted"}:
                    stats.failed_requests += 1
                stats.add_usage(entry.usage)
            elif isinstance(entry, MessageEntry) and entry.message["role"] == "assistant":
                # s11 新增：旧版 assistant 记录缺少模型元数据，标明历史统计不完整。
                stats.legacy_entries += 1
        return stats

    def add_usage(self, usage: ModelUsage | None) -> None:
        """只累加已知用量；缺失值单独计数。"""
        if usage is None:
            self.unknown_usage_requests += 1
        else:
            self.known_tokens += usage.total_tokens

    def summary_text(self) -> str:
        """返回可直接展示的会话累计摘要。"""
        suffix = (
            f"，{self.unknown_usage_requests} 次用量未知" if self.unknown_usage_requests else ""
        )
        legacy = f"；旧记录 {self.legacy_entries} 条未统计" if self.legacy_entries else ""
        return (
            f"会话  模型请求 {self.model_requests} 次"
            f"（摘要 {self.summary_requests} 次，失败 {self.failed_requests} 次），"
            f"工具 {self.tool_calls} 次，"
            f"已知 Token {self.known_tokens}{suffix}{legacy}"
        )


@dataclass(frozen=True)
class SessionErrorRecorder:
    """保存没有形成回答或摘要的失败模型调用。

    输入：当前会话和 model_error 事件；输出：一条 ModelErrorEntry。
    流程：请求器报告失败 → 观察者恢复可用 usage → 会话追加失败事实。
    边界：只保存抛异常的调用，不重复保存已经归属 assistant 的用量。
    """

    session: SessionManager

    def __call__(self, event: AgentEvent) -> None:
        """仅消费 model_error，其他事件由各自观察者处理。"""
        if event.type == "model_error":
            self.session.append_model_error(
                model=event.data["model"],
                purpose=event.data["purpose"],
                usage=_usage_from_record(event.data.get("usage")),
            )
