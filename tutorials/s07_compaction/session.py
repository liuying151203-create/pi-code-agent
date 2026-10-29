# ===== s06 新增 =====
# s06 新增；本章组件只相对导入本章，冻结当时运行行为。
"""s06：消息状态的所有者与追加式会话记录，参考 Pi Agent/SessionManager 的职责划分。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .code import agent_loop
from .compaction import CompactionResult, estimate_message_tokens, find_cut_point, summarize
from .events import AgentEvent, EventBus, EventListener
from .messages import (
    AssistantMessage,
    CompactionSummary,
    Message,
    Text,
    Thinking,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from .model import Model, StreamingModel
from .tools import Tool


# ===== s06 新增 =====
# s06 新增：持久化只写当前实际支持的数据，不反序列化 Python 对象或客户端。
def encode_message(message: Message) -> dict[str, Any]:
    """把已完成消息转为 JSON 数据，检查编码合法性，返回独立副本。"""
    # s07 新增：临时摘要可通过上下文校验，不能作为普通消息落盘。
    if isinstance(message, CompactionSummary):
        value = {"role": "compaction_summary", "content": message.content}
    elif isinstance(message, UserMessage):
        value = {"role": "user", "content": message.content}
    elif isinstance(message, ToolResultMessage):
        value = asdict(message)
    elif isinstance(message, AssistantMessage):
        if message.stop_reason == "pending":
            raise ValueError("pending 快照不能落盘")
        value = asdict(message)
        # s06 修改：持久化推理、签名和脱敏标记，恢复后仍能完整重放供应商协议。
        blocks = []
        for block in message.content:
            if isinstance(block, Text):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, Thinking):
                blocks.append({"type": "thinking", **asdict(block)})
            else:
                blocks.append(
                    {
                        "type": "tool_call",
                        "id": block.id,
                        "name": block.name,
                        "arguments": deepcopy(block.arguments),
                    }
                )
        value["content"] = blocks
    else:
        raise TypeError("不支持的会话消息")
    # s06 新增：验证读写契约一致，NaN 等非 JSON 数据不进入日志。
    value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    decode_message(value)
    return value


def decode_message(value: dict[str, Any]) -> Message:
    """验证文本/工具消息，重建本版本的消息类型；未知字段或类型直接拒绝。"""
    if not isinstance(value, dict):
        raise ValueError("消息必须是对象")  # noqa: TRY004 - 文件数据格式错误统一报告 ValueError
    role = value.get("role")
    # s07 新增：还原摘要类型供请求上下文校验，不改变真实用户消息。
    if role == "compaction_summary":
        if set(value) != {"role", "content"} or not isinstance(value["content"], str):
            raise ValueError("压缩摘要消息格式错误")
        return CompactionSummary(value["content"])
    if role == "user":
        if set(value) != {"role", "content"} or not isinstance(value["content"], str):
            raise ValueError("用户消息格式错误")
        return UserMessage(value["content"])
    if role == "tool_result":
        if set(value) != {"role", "tool_call_id", "tool_name", "content", "is_error"}:
            raise ValueError("工具结果字段错误")
        if (
            any(not isinstance(value[key], str) for key in ("tool_call_id", "tool_name", "content"))
            or type(value["is_error"]) is not bool
        ):
            raise ValueError("工具结果类型错误")
        if not value["tool_call_id"] or not value["tool_name"]:
            raise ValueError("工具结果缺少关联 ID 或名称")
        return ToolResultMessage(
            value["tool_call_id"], value["tool_name"], value["content"], value["is_error"]
        )
    if role != "assistant" or set(value) != {
        "role",
        "content",
        "stop_reason",
        "usage",
        "error_message",
    }:
        raise ValueError("模型消息字段错误")
    if not isinstance(value["stop_reason"], str) or value["stop_reason"] not in {
        "stop",
        "tool_use",
        "length",
        "error",
        "aborted",
    }:
        raise ValueError("只能恢复已完成的模型消息")
    if value["error_message"] is not None and not isinstance(value["error_message"], str):
        raise ValueError("模型错误说明必须是文本")
    if not isinstance(value["content"], list):
        raise ValueError("模型内容必须是列表")  # noqa: TRY004 - 文件数据格式错误统一报告 ValueError
    blocks = []
    for block in value["content"]:
        if not isinstance(block, dict):
            raise ValueError("模型内容块必须是对象")  # noqa: TRY004 - 文件数据格式错误统一报告 ValueError
        if (
            block.get("type") == "text"
            and set(block) == {"type", "text"}
            and isinstance(block["text"], str)
        ):
            blocks.append(Text(block["text"]))
        # s06 修改：只接受明确字段的推理块；旧版纯文本/工具消息继续按原格式恢复。
        elif (
            block.get("type") == "thinking"
            and set(block) == {"type", "text", "signature", "redacted"}
            and isinstance(block["text"], str)
            and isinstance(block["signature"], str)
            and type(block["redacted"]) is bool
        ):
            blocks.append(Thinking(block["text"], block["signature"], block["redacted"]))
        elif block.get("type") == "tool_call" and set(block) == {"type", "id", "name", "arguments"}:
            blocks.append(ToolCall(block["id"], block["name"], block["arguments"]))
        else:
            raise ValueError("不支持的模型内容块")
    usage = value["usage"]
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
            raise ValueError("用量格式错误")
        if any(type(count) is not int or count < 0 for count in usage.values()):
            raise ValueError("用量必须是非负整数")
        usage = Usage(**usage)
    return AssistantMessage(tuple(blocks), value["stop_reason"], usage, value["error_message"])


def timestamp() -> str:
    """生成 UTC 会话时间，避免依赖运行机器的本地时区。"""
    return datetime.now(UTC).isoformat()


def _reject_constant(value: str) -> None:
    """拒绝 JSON 标准之外的 NaN/Infinity 常量。"""
    raise ValueError(f"不支持的 JSON 常量：{value}")


# s07 新增：压缩起点必须是当前已有消息，且不能把工具结果单独留在开头。
def _validate_compaction(entry, previous_entries):
    """验证摘要、预算和保留起点；不修改已有历史。"""
    if not isinstance(entry["summary"], str) or not entry["summary"].strip():
        raise ValueError("压缩摘要不能为空")
    tokens = entry["tokens_before"]
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise ValueError("压缩前用量必须是非负整数或未知")
    usage = entry["usage"]
    if usage is not None and (
        not isinstance(usage, dict)
        or set(usage) != {"input_tokens", "output_tokens"}
        or any(type(value) is not int or value < 0 for value in usage.values())
    ):
        raise ValueError("摘要用量格式错误")
    keep = entry["first_kept_entry_id"]
    target = next((item for item in previous_entries if item["id"] == keep), None)
    if target is None or target["type"] != "message":
        raise ValueError("压缩保留起点必须指向已有消息")
    if isinstance(decode_message(target["message"]), ToolResultMessage):
        raise ValueError("压缩保留起点不能是工具结果")  # noqa: TRY004 - 文件记录内容错误
    # s07 新增：恢复时再次验证保留段，拒绝孤立或缺失的工具结果。
    start = previous_entries.index(target)
    outstanding = []
    for item in previous_entries[start:]:
        if item["type"] != "message":
            continue
        message = decode_message(item["message"])
        if isinstance(message, ToolResultMessage):
            if not outstanding or outstanding.pop(0) != (message.tool_call_id, message.tool_name):
                raise ValueError("压缩保留段的工具结果没有匹配的调用")
        else:
            if outstanding:
                raise ValueError("压缩保留段缺少工具结果")
            if isinstance(message, AssistantMessage) and message.stop_reason not in {
                "error",
                "aborted",
            }:
                outstanding = [
                    (block.id, block.name)
                    for block in message.content
                    if isinstance(block, ToolCall)
                ]
    if outstanding:
        raise ValueError("压缩保留段缺少工具结果")
    latest = next(
        (item for item in reversed(previous_entries) if item["type"] == "compaction"), None
    )
    if latest is not None:
        indices = {item["id"]: index for index, item in enumerate(previous_entries)}
        if indices[keep] <= indices[latest["first_kept_entry_id"]]:
            raise ValueError("压缩保留起点不能回退或重复")


# s06 新增：沿用 Pi 的头记录、条目 ID 和 parent 关联；本章只写一条线性分支。
class SessionStore:
    """追加式 JSONL 消息记录，落盘先于工具执行，不保存密钥或 Python 对象。

    输入：明确的路径与工作区；输出：恢复后的消息副本。
    流程：create/open → 每条已完成消息 append_message → 下次 open 恢复。
    边界：Python 线性格式支持 version=1/2，不直接导入 Pi 文件；损坏或写入失败时停止。
    """

    def __init__(self, path: Path, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        self._path = path
        self._header = deepcopy(header)
        self._entries = deepcopy(entries)
        self._size = path.stat().st_size
        # s07 新增：迁移前核对打开时的原始文件内容。
        self._snapshot = path.read_bytes()
        self.failed = False

    @property
    def path(self) -> Path:
        """返回已绑定的文件路径，不允许会话运行时切换存储目标。"""
        return self._path

    @property
    def header(self) -> dict[str, Any]:
        """返回会话元数据副本，不向外暴露可变内部状态。"""
        return deepcopy(self._header)

    @classmethod
    def create(cls, path: Path, workspace: Path) -> SessionStore:
        """独占创建头记录；已有文件绝不覆盖，父目录在明确路径下创建。"""
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("会话工作区必须是目录")
        path = path.resolve()
        header = {
            "type": "session",
            "version": 1,
            "id": uuid4().hex,
            "timestamp": timestamp(),
            "cwd": str(workspace),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(header, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return cls(path, header, [])

    @classmethod
    def open(cls, path: Path, workspace: Path) -> SessionStore:
        """校验头和条目链后恢复；错误不跳过、不截断、不静默改用其他文件。"""
        path = path.resolve(strict=True)
        # s07 修改：保留原始字节，供首次压缩迁移前检测外部变更。
        original = path.read_bytes()
        rows = [
            json.loads(line, parse_constant=_reject_constant)
            for line in original.decode("utf-8").splitlines()
        ]
        if not rows or not isinstance(rows[0], dict):
            raise ValueError("缺少会话头")
        header = rows[0]
        if (
            set(header) != {"type", "version", "id", "timestamp", "cwd"}
            or header["type"] != "session"
            or type(header["version"]) is not int
            or header["version"] not in {1, 2}
        ):
            raise ValueError("不支持的会话头或版本")
        if any(
            not isinstance(header[key], str) or not header[key]
            for key in ("id", "timestamp", "cwd")
        ):
            raise ValueError("会话头类型错误")
        if Path(header["cwd"]).resolve() != workspace.resolve(strict=True):
            raise ValueError("会话工作区不匹配")
        ids = set()
        parent = None
        for entry in rows[1:]:
            if not isinstance(entry, dict):
                raise ValueError("会话条目字段错误")  # noqa: TRY004 - 文件记录格式错误
            kind = entry.get("type")
            common = {"type", "id", "parent_id", "timestamp"}
            fields = {
                "message": {"message"},
                "compaction": {"summary", "first_kept_entry_id", "tokens_before", "usage"},
            }
            if (
                not isinstance(kind, str)
                or kind not in fields
                or set(entry) != common | fields[kind]
            ):
                raise ValueError("会话条目字段错误")
            if (
                (kind == "compaction" and header["version"] != 2)
                or not isinstance(entry["id"], str)
                or not entry["id"]
                or entry["id"] in ids
            ):
                raise ValueError("会话条目类型或 ID 错误")
            if entry["parent_id"] != parent or not isinstance(entry["timestamp"], str):
                raise ValueError("本章只支持连续的线性条目链")
            if entry["type"] == "message":
                if isinstance(decode_message(entry["message"]), CompactionSummary):
                    raise ValueError("临时摘要不能作为普通消息落盘")
            elif kind == "compaction":
                _validate_compaction(entry, rows[1 : len(ids) + 1])
            ids.add(entry["id"])
            parent = entry["id"]
        store = cls(path, header, rows[1:])
        store._snapshot = original
        store._size = len(original)
        return store

    @property
    def messages(self) -> list[Message]:
        """返回新的消息对象，调用方修改不会改变已落盘记录。"""
        return [
            decode_message(entry["message"])
            for entry in self._entries
            if entry["type"] == "message"
        ]

    @property
    def entries(self):
        """返回会话记录副本，供压缩准备与恢复使用。"""
        return deepcopy(self._entries)

    def append_message(self, message: Message) -> str:
        """先同步落盘再更新内存索引；写入结果不确定时锁住后续追加。"""
        if self.failed:
            raise RuntimeError("会话写入已失败，请检查文件后重新打开")
        if isinstance(message, CompactionSummary):
            raise ValueError("临时摘要不能作为普通消息落盘")  # noqa: TRY004 - 拒绝非法记录
        return self._append_entry({"type": "message", "message": encode_message(message)})

    # s07 新增：首次压缩前备份旧格式并原子替换，普通续聊不改版本。
    def append_compaction(self, summary, first_kept_entry_id, tokens_before, usage):
        """校验压缩记录并在必要时迁移版本，落盘成功后才更新记录。"""
        payload = {
            "type": "compaction",
            "summary": summary,
            "first_kept_entry_id": first_kept_entry_id,
            "tokens_before": tokens_before,
            "usage": asdict(usage) if usage is not None else None,
        }
        _validate_compaction(payload, self._entries)
        if self._header["version"] == 1:
            self._migrate_v2()
        return self._append_entry(payload)

    def _migrate_v2(self):
        """独占备份 v1，验证临时 v2 后替换；异常时不发布内存新版本。"""
        if self.failed:
            raise RuntimeError("会话写入已失败，请检查文件后重新打开")
        original = self.path.read_bytes()
        if original != self._snapshot:
            raise OSError("会话文件已被其他写入者改变")
        backup = self.path.with_name(self.path.name + ".v1.bak")
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        header = {**self._header, "version": 2}
        _, _, remainder = original.partition(b"\n")
        replacement = json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" + remainder
        try:
            with temporary.open("xb") as output:
                if output.write(replacement) != len(replacement):
                    raise OSError("新版会话文件未完整写入")
                output.flush()
                os.fsync(output.fileno())
            type(self).open(temporary, Path(header["cwd"]))
            with backup.open("xb") as output:
                if output.write(original) != len(original):
                    raise OSError("旧版会话备份未完整写入")
                output.flush()
                os.fsync(output.fileno())
            if self.path.read_bytes() != original:
                raise OSError("会话文件已被其他写入者改变")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._header = header
        self._snapshot = replacement
        self._size = len(replacement)

    # s07 修改：消息与压缩记录共用同一追加屏障和线性 parent 链。
    def _append_entry(self, payload: dict) -> str:
        """序列化后写入，成功才发布内存条目；写入不确定时使存储失效。"""
        if self.failed:
            raise RuntimeError("会话写入已失败，请检查文件后重新打开")
        entry = {
            **payload,
            "id": uuid4().hex[:12],
            "parent_id": self._entries[-1]["id"] if self._entries else None,
            "timestamp": timestamp(),
        }
        entry = json.loads(json.dumps(entry, ensure_ascii=False, allow_nan=False))
        encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        try:
            # s06 调整：Pi 为追加写；本实现拒绝文件消失或已发生的外部追加，不声称多进程事务。
            with self.path.open("r+b") as output:
                output.seek(0, os.SEEK_END)
                if output.tell() != self._size:
                    raise OSError("会话文件已被其他写入者改变")
                # s06 修复：合法的最后一行可以没有换行，先补分隔符再按 UTF-8 字节追加。
                output.seek(-1, os.SEEK_END)
                separator = b"" if output.read(1) == b"\n" else b"\n"
                output.seek(0, os.SEEK_END)
                data = separator + encoded.encode("utf-8")
                if output.write(data) != len(data):
                    raise OSError("会话记录未完整写入")
                output.flush()
                os.fsync(output.fileno())
                self._size = output.tell()
        except OSError:
            self.failed = True
            raise
        self._entries.append(entry)
        self._snapshot += data
        return entry["id"]


# s06 新增：会话持有消息和运行状态；低层循环仍可独立调用，客户端仍归创建者管理。
class AgentSession:
    """把模型、工具、事件和可选存储组装成一次可继续使用的会话。

    输入：模型、工具、可选存储和运行选项；输出：prompt 返回完整历史的独立副本。
    流程：恢复 → prompt 新输入 → 已完成消息落盘 → 收口 → 允许下一次输入。
    边界：拒绝并发 prompt；不创建/关闭客户端，不把流中 pending 快照写入文件。
    """

    def __init__(
        self,
        *,
        model: Model | StreamingModel,
        tools: Sequence[Tool] = (),
        store: SessionStore | None = None,
        stream: bool = False,
        system: str = "你是编程助手，完成任务后停止。",
        max_tokens: int = 8000,
        max_turns: int | None = None,
        keep_recent_tokens: int = 20000,
        loop: Callable = agent_loop,
    ) -> None:
        self.model, self.tools, self.store = model, tuple(tools), store
        self.stream, self.system, self.max_tokens, self.max_turns = (
            stream,
            system,
            max_tokens,
            max_turns,
        )
        self._loop = loop
        self._history = store.messages if store is not None else []
        self._saved = len(self._history)
        # s07 新增：内存会话也保存带 ID 的消息，供压缩定位。
        if type(keep_recent_tokens) is not int or keep_recent_tokens < 1:
            raise ValueError("近期保留预算必须是正整数")
        self.keep_recent_tokens = keep_recent_tokens
        self._memory_entries = []
        self._compacting = False
        self._busy = False
        self._active_task = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._storage_error = False
        self._events = EventBus()
        self._events.subscribe(self._record)

    @property
    def messages(self) -> list[Message]:
        """返回历史副本，包括运行中的当前快照，不允许外部改写内部列表。"""
        return deepcopy(self._history)

    @property
    def busy(self) -> bool:
        """直到末事件的订阅器和持久化收口完成才变为空闲。"""
        return self._busy

    # s07 新增：内存和磁盘记录都保留稳定 ID，供压缩定位。
    def _save_message(self, message):
        """先保存完成消息，再推进已保存数量。"""
        if self.store is not None:
            self.store.append_message(message)
        else:
            self._memory_entries.append(
                {
                    "type": "message",
                    "id": uuid4().hex[:12],
                    "message": encode_message(message),
                }
            )
        self._saved += 1

    def _journal_entries(self):
        """读取磁盘或内存记录副本；不暴露可修改的内部状态。"""
        return self.store.entries if self.store is not None else deepcopy(self._memory_entries)

    def _active_entries(self):
        """按最近压缩起点取可见消息，并保留上一次摘要。"""
        entries = self._journal_entries()
        latest = next((item for item in reversed(entries) if item["type"] == "compaction"), None)
        start = 0
        if latest is not None:
            start = next(
                i for i, item in enumerate(entries) if item["id"] == latest["first_kept_entry_id"]
            )
        messages = [
            {"id": item["id"], "message": decode_message(item["message"])}
            for item in entries[start:]
            if item["type"] == "message"
        ]
        return messages, latest

    def _request_context(self, messages):
        """生成临时摘要与近期消息，不改变完整历史或持久记录。"""
        active, latest = self._active_entries()
        if latest is None:
            return list(messages)
        return [CompactionSummary(latest["summary"]), *(item["message"] for item in active)]

    async def compact(self, instructions=None):
        """手动压缩历史；中止正在运行的回合，摘要写入后才用于模型请求。

        输入：可选摘要要求；输出：摘要、保留起点及实际用量。
        失败或取消不写压缩记录；完整会话消息始终保留。
        """
        if instructions is not None and not isinstance(instructions, str):
            raise TypeError("摘要要求必须是文本或 None")
        if getattr(self, "_closed", False):
            raise RuntimeError("会话已关闭")
        if self._compacting:
            raise RuntimeError("会话正在压缩")
        if self.busy:
            if asyncio.current_task() is self._active_task:
                raise RuntimeError("活跃处理函数不能等待自己空闲")
            self.abort()
            await self.wait_for_idle()
        if self.busy:
            raise RuntimeError("会话正在运行")
        if self._storage_error or (self.store is not None and self.store.failed):
            raise RuntimeError("会话存储已失败，请检查并重新打开")
        self._busy = True
        self._compacting = True
        self._idle.clear()
        self._active_task = asyncio.current_task()
        try:
            entries = self._journal_entries()
            last_compaction = next(
                (i for i in range(len(entries) - 1, -1, -1) if entries[i]["type"] == "compaction"),
                None,
            )
            if last_compaction is not None and not any(
                entry["type"] == "message" for entry in entries[last_compaction + 1 :]
            ):
                raise ValueError("没有新的可压缩消息")
            active, latest = self._active_entries()
            cut = find_cut_point(active, self.keep_recent_tokens)
            if cut is None:
                raise ValueError("没有足够的可压缩消息")
            before = sum(estimate_message_tokens(item["message"]) for item in active)
            previous = latest["summary"] if latest else None
            if previous:
                before += estimate_message_tokens(CompactionSummary(previous))
            await self._events.emit({"type": "compaction_start"})
            removed = [item["message"] for item in active[: cut.first_kept]]
            groups = [(removed, previous)]
            if cut.split_turn_start is not None:
                earlier = removed[: cut.split_turn_start]
                turn = removed[cut.split_turn_start :]
                if earlier or previous:
                    groups = [(earlier, previous), (turn, None)]
            summaries, usages = [], []
            for group, old_summary in groups:
                summary, usage = await summarize(
                    self.model, group, old_summary, instructions, self.max_tokens, encode_message
                )
                summaries.append(summary)
                usages.append(usage)
            summary = "\n\n".join(summaries)
            usage = (
                Usage(
                    sum(item.input_tokens for item in usages),
                    sum(item.output_tokens for item in usages),
                )
                if all(item is not None for item in usages)
                else None
            )
            first_id = active[cut.first_kept]["id"]
            if self.store is not None:
                self.store.append_compaction(summary, first_id, before, usage)
            else:
                entry = {
                    "type": "compaction",
                    "id": uuid4().hex[:12],
                    "summary": summary,
                    "first_kept_entry_id": first_id,
                    "tokens_before": before,
                    "usage": asdict(usage) if usage else None,
                }
                _validate_compaction(entry, self._memory_entries)
                self._memory_entries.append(entry)
            result = CompactionResult(summary, first_id, before, usage)
            await self._events.emit({"type": "compaction_end", "result": result})
            return result
        finally:
            self._compacting = False
            self._busy = False
            self._active_task = None
            self._idle.set()

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """订阅可等待事件，内置落盘订阅先于外部订阅。"""
        return self._events.subscribe(listener)

    def _record(self, event: AgentEvent) -> None:
        """只记录完成消息，确保消息持久化屏障先于工具副作用。"""
        # s07 修改：完成消息无论是否落盘，都先写入会话记录。
        if event["type"] == "message_end":
            try:
                self._save_message(event["message"])
            except Exception:
                self._storage_error = True
                raise

    # s07 新增：手动压缩先取消当前任务，再等待其收口。
    def abort(self):
        """取消当前回合；没有活跃任务时不做操作。"""
        if self._active_task is not None:
            self._active_task.cancel()

    async def wait_for_idle(self):
        """等待运行收口；禁止活跃处理函数等待自身。"""
        if self.busy and asyncio.current_task() is self._active_task:
            raise RuntimeError("活跃处理函数不能等待自己空闲")
        await self._idle.wait()

    async def prompt(self, text: str) -> list[Message]:
        """加入一次用户输入并运行；结束或失败时补存尚未通知的已完成消息。"""
        if self._busy:
            raise RuntimeError("会话正在运行，请等待当前回合完成")
        if not isinstance(text, str):
            raise TypeError("用户输入必须是文本")
        if self._storage_error or (self.store is not None and self.store.failed):
            raise RuntimeError("会话存储已失败，请检查并重新打开")
        self._busy = True
        self._idle.clear()
        self._active_task = asyncio.current_task()
        try:
            await self._loop(
                self._history,
                model=self.model,
                tools=self.tools,
                system=self.system,
                max_tokens=self.max_tokens,
                max_turns=self.max_turns,
                prompts=[UserMessage(text)],
                events=self._events,
                stream=self.stream,
                transform_context=self._request_context,
            )
            return self.messages
        finally:
            try:
                if (self.store is None or not self.store.failed) and not self._storage_error:
                    # s06 新增：观察者中断可能没有后续 message_end，仍保存已执行结果和补齐结果。
                    completed = [
                        message
                        for message in self._history
                        if not (
                            isinstance(message, AssistantMessage)
                            and message.stop_reason == "pending"
                        )
                    ]
                    for message in completed[self._saved :]:
                        self._save_message(message)
            finally:
                self._busy = False
                self._active_task = None
                self._idle.set()
