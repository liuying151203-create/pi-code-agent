"""s06：消息状态的所有者与追加式会话记录，参考 Pi Agent/SessionManager 的职责划分。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pi_code_agent.events import AgentEvent, EventBus, EventListener
from pi_code_agent.extensions import ExtensionRuntime
from pi_code_agent.loop import agent_loop
from pi_code_agent.messages import (
    AssistantMessage,
    Message,
    Text,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_code_agent.model import Model, StreamingModel
from pi_code_agent.tools import Tool


# ===== s06 新增 =====
# s06 新增：持久化只写当前实际支持的数据，不反序列化 Python 对象或客户端。
def encode_message(message: Message) -> dict[str, Any]:
    """把已完成消息转为 JSON 数据，检查编码合法性，返回独立副本。"""
    if isinstance(message, UserMessage):
        value = {"role": "user", "content": message.content}
    elif isinstance(message, ToolResultMessage):
        value = asdict(message)
    elif isinstance(message, AssistantMessage):
        if message.stop_reason == "pending":
            raise ValueError("pending 快照不能落盘")
        value = asdict(message)
        value["content"] = [
            {"type": "text", "text": block.text}
            if isinstance(block, Text)
            else {
                "type": "tool_call",
                "id": block.id,
                "name": block.name,
                "arguments": deepcopy(block.arguments),
            }
            for block in message.content
        ]
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


# s06 新增：沿用 Pi 的头记录、条目 ID 和 parent 关联；本章只写一条线性分支。
class SessionStore:
    """追加式 JSONL 消息记录，落盘先于工具执行，不保存密钥或 Python 对象。

    输入：明确的路径与工作区；输出：恢复后的消息副本。
    流程：create/open → 每条已完成消息 append_message → 下次 open 恢复。
    边界：Python 格式 version=1，不兼容直接导入 Pi 文件；损坏或写入失败时停止。
    """

    def __init__(self, path: Path, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        self._path = path
        self._header = deepcopy(header)
        self._entries = deepcopy(entries)
        self._size = path.stat().st_size
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
        with path.open(encoding="utf-8") as source:
            rows = [json.loads(line, parse_constant=_reject_constant) for line in source]
        if not rows or not isinstance(rows[0], dict):
            raise ValueError("缺少会话头")
        header = rows[0]
        if (
            set(header) != {"type", "version", "id", "timestamp", "cwd"}
            or header["type"] != "session"
            or type(header["version"]) is not int
            or header["version"] != 1
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
            if not isinstance(entry, dict) or set(entry) != {
                "type",
                "id",
                "parent_id",
                "timestamp",
                "message",
            }:
                raise ValueError("会话条目字段错误")
            if (
                entry["type"] != "message"
                or not isinstance(entry["id"], str)
                or not entry["id"]
                or entry["id"] in ids
            ):
                raise ValueError("会话条目类型或 ID 错误")
            if entry["parent_id"] != parent or not isinstance(entry["timestamp"], str):
                raise ValueError("本章只支持连续的线性条目链")
            decode_message(entry["message"])
            ids.add(entry["id"])
            parent = entry["id"]
        return cls(path, header, rows[1:])

    @property
    def messages(self) -> list[Message]:
        """返回新的消息对象，调用方修改不会改变已落盘记录。"""
        return [decode_message(entry["message"]) for entry in self._entries]

    def append_message(self, message: Message) -> str:
        """先同步落盘再更新内存索引；写入结果不确定时锁住后续追加。"""
        if self.failed:
            raise RuntimeError("会话写入已失败，请检查文件后重新打开")
        entry = {
            "type": "message",
            "id": uuid4().hex[:12],
            "parent_id": self._entries[-1]["id"] if self._entries else None,
            "timestamp": timestamp(),
            "message": encode_message(message),
        }
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
        # s07 新增：内置工具与 SDK 显式工具分开，保留 Pi 的覆盖优先级。
        builtin_tools: Sequence[Tool] = (),
        extensions: ExtensionRuntime | None = None,
        store: SessionStore | None = None,
        # s08 新增：上下文使用明确业务工作区，不从插件文件推断。
        cwd: Path | None = None,
        stream: bool = False,
        system: str = "你是编程助手，完成任务后停止。",
        max_tokens: int = 8000,
        max_turns: int | None = None,
        loop: Callable = agent_loop,
    ) -> None:
        self.model, self.store = model, store
        self.extensions = extensions
        self.cwd = Path(cwd or (store.header["cwd"] if store else Path.cwd())).resolve()
        self.tools = tuple(
            extensions.resolve_tools(builtin_tools, sdk_tools=tools)
            if extensions is not None
            else list({tool.name: tool for tool in [*builtin_tools, *tools]}.values())
        )
        self.stream, self.system, self.max_tokens, self.max_turns = (
            stream,
            system,
            max_tokens,
            max_turns,
        )
        self._loop = loop
        self._history = store.messages if store is not None else []
        self._saved = len(self._history)
        self._busy = False
        self._storage_error = False
        self._events = EventBus()
        self._events.subscribe(self._record)
        if extensions is not None:
            self._events.subscribe(self._observe_extensions)

    @property
    def messages(self) -> list[Message]:
        """返回历史副本，包括运行中的当前快照，不允许外部改写内部列表。"""
        return deepcopy(self._history)

    @property
    def busy(self) -> bool:
        """直到末事件的订阅器和持久化收口完成才变为空闲。"""
        return self._busy

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """订阅可等待事件，内置落盘订阅先于外部订阅。"""
        return self._events.subscribe(listener)

    def _record(self, event: AgentEvent) -> None:
        """只记录完成消息，确保消息持久化屏障先于工具副作用。"""
        if self.store is not None and event["type"] == "message_end":
            try:
                self.store.append_message(event["message"])
            except Exception:
                self._storage_error = True
                raise
            self._saved += 1

    # s08 新增：普通事件 Hook 在持久化之后观察，不更改已保存消息。
    async def _observe_extensions(self, event):
        """向扩展发送生命周期事件，订阅器的返回值不修改运行历史。"""
        await self.extensions.dispatch(event, self)

    async def _transform_context(self, messages):
        """生成每次模型请求的临时上下文，保留会话真实历史。"""
        result = await self.extensions.dispatch({"type": "context", "messages": messages}, self)
        return result["messages"]

    async def _before_tool(self, call, arguments):
        """把校验后的参数交给拦截器；异常交由工具执行层转为失败。"""
        return await self.extensions.dispatch(
            {
                "type": "tool_call",
                "tool_name": call.name,
                "tool_call_id": call.id,
                "input": arguments,
            },
            self,
        )

    async def _after_tool(self, call, result):
        """串联工具结果覆盖，保留明确的错误状态与终止选择。"""
        from pi_code_agent.tools import ToolResult

        event = await self.extensions.dispatch(
            {
                "type": "tool_result",
                "tool_name": call.name,
                "tool_call_id": call.id,
                "content": result.content,
                "is_error": result.is_error,
                "terminate": result.terminate,
            },
            self,
        )
        return ToolResult(event["content"], event["is_error"], event["terminate"])

    async def prompt(self, text: str) -> list[Message]:
        """加入一次用户输入并运行；结束或失败时补存尚未通知的已完成消息。"""
        if self._busy:
            raise RuntimeError("会话正在运行，请等待当前回合完成")
        if not isinstance(text, str):
            raise TypeError("用户输入必须是文本")
        if self._storage_error or (self.store is not None and self.store.failed):
            raise RuntimeError("会话存储已失败，请检查并重新打开")
        self._busy = True
        try:
            policies = {}
            if self.extensions is not None:
                event = await self.extensions.dispatch(
                    {"type": "input", "text": text, "source": "sdk"}, self
                )
                if event.get("action") == "handled":
                    return self.messages
                text = event["text"]
                policies = {
                    "transform_context": self._transform_context,
                    "before_tool_call": self._before_tool,
                    "after_tool_call": self._after_tool,
                }
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
                **policies,
            )
            return self.messages
        finally:
            try:
                if self.store is not None and not self.store.failed and not self._storage_error:
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
                        self.store.append_message(message)
                        self._saved += 1
            finally:
                self._busy = False
