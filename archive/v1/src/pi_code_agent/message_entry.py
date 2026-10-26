"""来自 s06：保持原始消息记录与规范化规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pi_code_agent import serialization as previous

Message = previous.Message


def _normalize_message(message: Message) -> Message:
    """将消息标准化为可写入 JSON 的 role/content 字典。"""
    role = message.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("会话消息的 role 必须是非空字符串")
    return {
        "role": role,
        "content": previous._to_jsonable(message.get("content", "")),
    }


def _message_from_record(record: object) -> Message:
    """校验 JSON 记录中的消息字段并恢复为模型消息。"""
    if not isinstance(record, dict):
        raise TypeError("消息记录必须是对象")
    role = record.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("会话消息的 role 必须是非空字符串")
    return {"role": role, "content": record.get("content", "")}


@dataclass(frozen=True)
class MessageEntry:
    """记录一条原始 Agent 消息。

    作用：保存用户、助手和工具结果消息，作为会话的完整事实记录。
    输入：标准模型消息及可选时间戳。
    输出：可追加到 JSONL、也可直接投影为模型消息的 Entry。
    """

    # s06 新增：s05 的 SessionMessage 改为带 type 的 MessageEntry，供统一 Entry 日志使用。
    message: Message
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_message(cls, message: Message) -> MessageEntry:
        """从运行时消息创建可持久化 Entry。"""
        return cls(message=_normalize_message(message))

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> MessageEntry:
        """读取 s06 嵌套消息 Record。"""
        # s06 修改：s06 Record 必须包含嵌套 message 字段，保持日志结构单一。
        raw_message = record.get("message")
        return cls(
            message=_message_from_record(raw_message),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """转换为 s06 的嵌套消息 JSONL 记录。"""
        return {
            "type": "message",
            "message": self.message,
            "timestamp": self.timestamp,
        }
