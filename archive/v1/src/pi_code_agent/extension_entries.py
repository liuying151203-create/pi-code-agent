"""s18：参考 Pi 区分扩展状态与模型消息，许可与来源见 docs/third-party-notices.md。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from pi_code_agent.serialization import Message


def _validate_custom_type(custom_type: str) -> None:
    """扩展自行命名状态或消息类型，空名称不能用于恢复。"""
    if not isinstance(custom_type, str) or not custom_type.strip():
        raise ValueError("custom_type 必须是非空字符串")


# s18 新增：对应 Pi CustomEntry，数据参与恢复，不参与模型上下文。
@dataclass(frozen=True)
class CustomEntry:
    """持久化扩展自己的 JSON 状态。

    输入：类型名称与 JSON 数据；输出：custom 记录。
    流程：append_entry 创建并复制 → 会话保存 → 扩展从完整日志恢复。
    边界：通用会话不解释状态含义，压缩不删除原记录。
    """

    custom_type: str
    data: Any = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        _validate_custom_type(self.custom_type)
        json.dumps(self.data, ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, "data", deepcopy(self.data))

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CustomEntry:
        """恢复扩展状态；不执行扩展。"""
        return cls(record["custom_type"], record.get("data"), record["timestamp"])

    def to_record(self) -> dict[str, Any]:
        """生成独立 JSON 状态记录。"""
        return {"type": "custom", **asdict(self)}


# s18 新增：对应 Pi CustomMessageEntry，display 只控制展示，details 不发送模型。
@dataclass(frozen=True)
class CustomMessageEntry:
    """保存扩展明确发送的模型可见文本与独立元数据。

    输入：类型、文本、展示开关和 JSON 元数据；输出：custom_message 记录及 user 投影。
    流程：保存 → 按 display 发出展示 → 下次请求或主动触发时投影 content。
    边界：延续当前文本 SDK；与 Pi 的多模态差异留到多模态章节，编号待定。
    """

    custom_type: str
    content: str
    display: bool = True
    details: Any = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        _validate_custom_type(self.custom_type)
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("扩展消息 content 必须是非空字符串")
        if type(self.display) is not bool:
            raise TypeError("display 必须是布尔值")
        json.dumps(self.details, ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, "details", deepcopy(self.details))

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CustomMessageEntry:
        """恢复明确发送的消息，缺失展示开关时报告损坏记录。"""
        return cls(
            custom_type=record["custom_type"],
            content=record["content"],
            display=record["display"],
            details=record.get("details"),
            timestamp=record["timestamp"],
        )

    def to_record(self) -> dict[str, Any]:
        """保存正文及元数据，不把 details 混入 content。"""
        return {"type": "custom_message", **asdict(self)}

    def to_message(self) -> Message:
        """按 Pi convertToLlm 的语义，只投影正文为 user 消息。"""
        return {"role": "user", "content": self.content}
