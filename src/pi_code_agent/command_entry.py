"""s18：命令执行审计，与模型可见消息分开保存。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


# s18 新增：保留人工执行事实；参考 Pi 的消息分层，审计本身不成为模型消息。
@dataclass(frozen=True)
class CommandEntry:
    """保存命令开始和处理结果，不触发重放或模型投影。

    输入：执行 ID、命令、状态及可选错误；输出：command JSONL 记录。
    流程：执行前 started → 结束后 succeeded/failed；缺少结束表示完成情况未知。
    边界：只负责审计，实际效果另存 CustomEntry 或 CustomMessageEntry。
    """

    command_id: str
    command: str
    status: Literal["started", "succeeded", "failed"]
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        for name in ("command_id", "command", "timestamp"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"命令记录 {name} 必须是非空字符串")
        if not self.command.startswith("/"):
            raise ValueError("命令记录必须以 / 开头")
        if self.status not in {"started", "succeeded", "failed"}:
            raise ValueError("命令状态必须为 started、succeeded 或 failed")
        if self.error is not None and (not isinstance(self.error, str) or not self.error.strip()):
            raise ValueError("命令错误必须是非空字符串或 None")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("只有 failed 记录必须包含错误说明")

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CommandEntry:
        """从 JSONL 恢复审计，缺失字段由存储层连同行号报告。"""
        # s18 修改：初版失败记录使用 model_text 保存错误，只恢复为审计，不重发给模型。
        error = record.get("error")
        if record["status"] == "failed" and error is None:
            error = record.get("model_text")
        return cls(
            command_id=record["command_id"],
            command=record["command"],
            status=record["status"],
            error=error,
            timestamp=record["timestamp"],
        )

    def to_record(self) -> dict[str, Any]:
        """转换为独立审计记录，不生成 user 或 tool_result 消息。"""
        return {"type": "command", **asdict(self)}
