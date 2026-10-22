"""来自 s11：事件分发、终端展示、Trace 与运行用量。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from pi_code_agent import serialization as s05
from pi_code_agent.serialization import Message, _get
from pi_code_agent.ui import (
    format_assistant_message,
    format_model_request,
    format_tool_call,
    format_tool_result,
)

TRACE_ROOT = Path(".traces/pi-code-agent")

type AgentEventType = Literal[
    "agent_start",
    "model_request",
    "model_response",
    "model_error",
    "summary_empty",
    "assistant_delta",
    "assistant_message",
    "tool_call",
    "tool_result",
    "compaction",
    "agent_end",
]


@dataclass(frozen=True)
class AgentEvent:
    """描述 Agent 运行过程中已经发生的一项事实。

    作用：在运行组件与终端、Trace、指标统计之间建立只读事件边界。
    输入：稳定的事件类型，以及该事件对应的数据。
    输出：可发送给一个或多个 `EventSink` 的不可变事件对象。
    流程：运行组件创建事件 → EventDispatcher 广播 → 各观察者独立消费。
    """

    type: AgentEventType
    data: dict[str, Any]


type EventSink = Callable[[AgentEvent], None]


class EventDispatchError(RuntimeError):
    """标记事件监听器失败，要求当前运行入口停止。

    输入：失败事件及监听器异常说明；输出：保留原异常链的致命运行时错误。
    边界：不吞掉 Trace 故障、不重新广播失败事件；已保存事实由会话恢复规则处理。
    """


@dataclass
class EventDispatcher:
    """按注册顺序把同一个事件发送给多个观察者。

    作用：提供最小的事件多播能力，让新增观察者不需要修改 Agent loop。
    输入：实现 `EventSink` 签名的监听器列表，以及运行时发出的 `AgentEvent`。
    输出：无；每个监听器都会按注册顺序收到同一个事件。
    流程：注册监听器 → 核心组件调用 `emit()` → 依次通知监听器。

    监听器失败会转换为 EventDispatchError 并停止入口，不静默丢失 Trace。
    """

    listeners: list[EventSink] = field(default_factory=list)

    def subscribe(self, listener: EventSink) -> None:
        """注册一个事件监听器。"""
        self.listeners.append(listener)

    def emit(self, event: AgentEvent) -> None:
        """按注册顺序广播一个事件。"""
        for listener in tuple(self.listeners):
            try:
                listener(event)
            except EventDispatchError:
                raise
            except Exception as error:
                # s11 修复 B019：同一失败不再广播，保留原因并让入口停止而非继续循环。
                raise EventDispatchError(
                    f"事件 {event.type} 的观察者失败，已停止运行：{error}"
                ) from error


@dataclass
class TerminalEventSink:
    """把用户可见的运行事件渲染到终端。

    作用：保持 s10 的流式显示行为，并忽略只供 Trace 和统计使用的内部事件。
    输入：按发生顺序到达的 `AgentEvent`。
    输出：无；用户可见事件写入标准输出。
    流程：显示模型请求和文本增量 → 显示工具与压缩状态 → 完整消息结束流式行。
    """

    _streaming_answer: bool = False

    def __call__(self, event: AgentEvent) -> None:
        """消费一个事件，并维护流式回答的行状态。"""
        if event.type == "model_request":
            print(format_model_request(event.data["timeout_seconds"]), flush=True)
        elif event.type == "model_error":
            self._finish_streamed_answer()
        elif event.type == "assistant_delta":
            self._write_delta(str(event.data["text"]))
        elif event.type == "assistant_message":
            self._write_complete_message(event.data["message"])
        elif event.type == "tool_call":
            print(
                format_tool_call(event.data["name"], event.data["arguments"]),
                flush=True,
            )
        elif event.type == "tool_result":
            print(format_tool_result(event.data["output"]), flush=True)
        elif event.type == "compaction":
            # 来自 s07：保持；只展示已成功保存的模型摘要，空摘要不会产生压缩事件。
            print(
                "会话  已压缩 "
                f"{event.data['before_chars']} → {event.data['after_chars']} 字符，"
                f"总结 {event.data['summarized_messages']} 条、"
                f"保留 {event.data['retained_messages']} 条，使用模型摘要。",
                flush=True,
            )

    def _write_delta(self, text: str) -> None:
        """追加文本片段，并只在首个片段前打印 Agent 前缀。"""
        if not text:
            return
        if not self._streaming_answer:
            print("Agent  ", end="", flush=True)
            self._streaming_answer = True
        print(text.replace("\n", "\n       "), end="", flush=True)

    def _write_complete_message(self, message: Message) -> None:
        """结束增量输出；没有增量时一次性显示完整消息。"""
        if self._finish_streamed_answer():
            return
        text = s05._text_from_content(message["content"])
        if text:
            print(format_assistant_message(text), flush=True)

    def _finish_streamed_answer(self) -> bool:
        """结束正在输出的回答行，并返回此前是否存在增量文本。"""
        if not self._streaming_answer:
            return False
        print(flush=True)
        self._streaming_answer = False
        return True


@dataclass
class RunStats:
    """保存当前一次 Agent 运行累计得到的指标。"""

    model_requests: int = 0
    summary_requests: int = 0
    model_responses: int = 0
    failed_requests: int = 0
    unknown_usage_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_duration_ms: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        """返回当前运行累计 Token 数。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass
class UsageTracker:
    """从运行事件累计模型、工具和耗时指标。

    作用：为一次自然语言任务生成稳定的运行统计，不侵入 Agent loop 和工具 Hook。
    输入：EventDispatcher 按顺序发送的运行事件。
    输出：最新指标保存在 `stats`；`summary_text()` 返回终端摘要。
    流程：agent_start 重置 → 模型和工具事件累加 → agent_end 计算总耗时。
    """

    stats: RunStats = field(default_factory=RunStats)
    _started_at: float | None = None

    def __call__(self, event: AgentEvent) -> None:
        """消费一个事件并更新当前运行指标。"""
        if event.type == "agent_start":
            self.stats = RunStats()
            self._started_at = perf_counter()
        elif event.type == "model_request":
            self.stats.model_requests += 1
            if event.data["purpose"] == "summary":
                self.stats.summary_requests += 1
        elif event.type == "model_response":
            self.stats.model_responses += 1
            # s11 修改：模型返回错误状态也计入用量；缺失 usage 单独计数。
            if event.data.get("stop_reason") in {"error", "aborted"}:
                self.stats.failed_requests += 1
            self._add_usage(event.data.get("usage"))
        elif event.type == "model_error":
            # s11 新增：抛异常的请求可能已经产生可获知的 Token 用量。
            self.stats.failed_requests += 1
            self._add_usage(event.data.get("usage"))
        elif event.type == "summary_empty":
            # s11 新增：模型虽返回但未生成可用摘要，只增加失败次数，不重复计费。
            self.stats.failed_requests += 1
        elif event.type == "tool_call":
            self.stats.tool_calls += 1
        elif event.type == "tool_result":
            self.stats.tool_duration_ms += float(event.data["duration_ms"])
            if event.data["is_error"]:
                self.stats.tool_errors += 1
        elif event.type == "agent_end" and self._started_at is not None:
            self.stats.elapsed_ms = (perf_counter() - self._started_at) * 1000

    def summary_text(self) -> str:
        """返回一行便于阅读的当前运行摘要。"""
        # s11 修正：模型异常会跳过 agent_end，仍显示从本轮开始到现在的耗时。
        elapsed_ms = self.stats.elapsed_ms
        if elapsed_ms == 0 and self._started_at is not None:
            elapsed_ms = (perf_counter() - self._started_at) * 1000
        unknown = (
            f"，{self.stats.unknown_usage_requests} 次用量未知"
            if self.stats.unknown_usage_requests
            else ""
        )
        return (
            f"运行  模型请求 {self.stats.model_requests} 次"
            f"（摘要 {self.stats.summary_requests} 次，失败 {self.stats.failed_requests} 次），"
            f"工具 {self.stats.tool_calls} 次，"
            f"已知 Token {self.stats.total_tokens}{unknown}，"
            f"耗时 {elapsed_ms / 1000:.2f} 秒"
        )

    def _add_usage(self, usage: dict[str, Any] | None) -> None:
        """累计已知用量，并区分完全缺失的 usage。"""
        if usage is None:
            self.stats.unknown_usage_requests += 1
            return
        self.stats.input_tokens += int(usage["input_tokens"])
        self.stats.output_tokens += int(usage["output_tokens"])
        self.stats.cache_read_tokens += int(usage["cache_read_tokens"])
        self.stats.cache_write_tokens += int(usage["cache_write_tokens"])


@dataclass(frozen=True)
class JsonlTraceRecorder:
    """把运行事件的紧凑投影追加到 JSONL 文件。

    作用：保存可复盘、可评测的运行事实，同时避免复制完整会话和大型工具结果。
    输入：Trace 文件路径和按顺序到达的 `AgentEvent`。
    输出：每个非增量事件写成一行 JSON；文本 delta 不单独落盘。
    流程：裁剪事件数据 → 添加 UTC 时间 → 追加 JSONL；完整消息仍由 Session 保存。
    """

    path: Path

    def __call__(self, event: AgentEvent) -> None:
        """把一个非增量事件写入 Trace。"""
        # s11 新增：逐 Token/文本片段落盘会制造大量重复数据，完整消息由会话保存。
        if event.type == "assistant_delta":
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event.type,
            "data": _trace_event_data(event),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_trace_path(trace_root: Path = TRACE_ROOT) -> Path:
    """为当前进程生成一个不会覆盖旧记录的 Trace 路径。"""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return trace_root / f"trace-{timestamp}.jsonl"


def _trace_event_data(event: AgentEvent) -> dict[str, Any]:
    """生成适合长期保存的紧凑事件数据。"""
    if event.type == "assistant_message":
        content = event.data["message"].get("content", [])
        blocks = content if isinstance(content, list) else []
        return {
            "content_types": [str(_get(block, "type")) for block in blocks],
            "text_chars": sum(
                len(str(_get(block, "text") or ""))
                for block in blocks
                if _get(block, "type") == "text"
            ),
        }
    if event.type == "tool_result":
        output = str(event.data["output"])
        return {
            "tool_call_id": event.data["tool_call_id"],
            "name": event.data["name"],
            "is_error": event.data["is_error"],
            "duration_ms": event.data["duration_ms"],
            "output_chars": len(output),
            "output_preview": output[:500],
        }
    if event.type == "agent_end":
        data: dict[str, Any] = {"message_count": len(event.data["messages"])}
        # s11 修复 B021：停止原因是紧凑运行事实，不能随完整消息一起丢弃。
        if "reason" in event.data:
            data["reason"] = event.data["reason"]
        return data
    return s05._to_jsonable(event.data)
