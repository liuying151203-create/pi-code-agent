"""s15 修改：沿用 s11 会话格式，仅给工具结果消息增加独立元数据。"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from s11_runtime_observability import code as runtime

# 来自 s14：保持；消息、摘要和用量仍使用已有运行时类型。
Message = runtime.Message
# s15 新增：同批状态按工具调用 ID 归属，并由专用回调和正文一起保存。
type ToolDetails = dict[str, dict[str, Any]]
type ToolResultSaver = Callable[[Message, ToolDetails], None]


# s15 新增：持久化失败与普通模型/工具错误区分，不能带未保存状态继续下一任务。
class SessionPersistenceError(RuntimeError):
    """工具结果未可靠保存时，要求入口停止，不发送成功进度提示。"""


def _validate_and_copy_tool_details(message: Message, tool_details: ToolDetails) -> ToolDetails:
    """校验元数据确实属于当前工具结果，并复制 JSON 状态。"""
    if not isinstance(tool_details, dict):
        raise TypeError("tool_details 必须是对象")
    if not tool_details:
        return {}
    if message.get("role") != "user" or not isinstance(message.get("content"), list):
        raise ValueError("tool_details 只能属于工具结果消息")
    result_ids = {
        block.get("tool_use_id")
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    for call_id, value in tool_details.items():
        if not isinstance(call_id, str) or not call_id or call_id not in result_ids:
            raise ValueError("tool_details 必须使用当前工具结果的调用 ID")
        if not isinstance(value, dict) or set(value) != {"name", "details"}:
            raise ValueError("工具元数据必须包含 name 和 details")
        if not isinstance(value["name"], str) or not value["name"]:
            raise ValueError("工具元数据 name 必须是非空字符串")
        if not isinstance(value["details"], dict):
            raise TypeError("工具状态 details 必须是对象")
    json.dumps(tool_details, ensure_ascii=False, allow_nan=False)
    return deepcopy(tool_details)


# s15 修改：沿用 s11 MessageEntry，在记录层增加状态，不修改模型 message 结构。
@dataclass(frozen=True)
class MessageEntry(runtime.MessageEntry):
    """保存正文消息，以及按工具调用 ID 对应的状态元数据。

    输入：原有 message、模型用量，以及可选 tool_details。
    输出：JSONL 记录；构建模型上下文时仍只投影 message。
    流程：普通字段复用 s11 → 校验并读写 tool_details → 交给会话恢复状态。
    不新增任务专用 Entry，循环和会话层都不理解 Todo 的字段含义。
    """

    tool_details: ToolDetails = field(default_factory=dict)

    # s15 修改：原有消息、模型和用量的读取不变，附加读取工具状态字段。
    @classmethod
    def from_record(cls, record: dict[str, Any]) -> MessageEntry:
        """恢复消息及可选元数据，字段无效就报告而不是清空状态。"""
        base = runtime.MessageEntry.from_record(record)
        tool_details = _validate_and_copy_tool_details(base.message, record.get("tool_details", {}))
        return cls(
            message=base.message,
            timestamp=base.timestamp,
            model=base.model,
            stop_reason=base.stop_reason,
            usage=base.usage,
            tool_details=tool_details,
        )

    def to_record(self) -> dict[str, Any]:
        """正文仍位于 message；状态只保存在记录顶层，不进入模型 API。"""
        record = super().to_record()
        # s15 新增：没有状态时不增加空字段，保留普通消息的简洁格式。
        if self.tool_details:
            record["tool_details"] = _validate_and_copy_tool_details(
                self.message, self.tool_details
            )
        return record


# 来自 s11：保持；压缩和失败调用记录不增加扩展字段。
type SessionEntry = MessageEntry | runtime.CompactionEntry | runtime.ModelErrorEntry


# s15 修改：替换消息读取器以保留状态字段，追加写入仍复用 s11。
class JsonlSessionStore(runtime.JsonlSessionStore):
    """读取含工具状态的本章会话，追加写入仍复用 s11。

    输入：会话路径；输出：消息、压缩和模型失败 Entry。
    流程：逐行解析 → 消息交给 s15 MessageEntry → 其他记录交给 s11。
    格式错误包含行号，不自动迁移或忽略损坏记录。
    """

    # s15 修改：只替换消息解析器，其他两种记录继续用 s11 的实现。
    def read_all(self) -> list[SessionEntry]:
        """读取完整日志，工具状态不受当前压缩点影响。"""
        if not self.path.exists():
            return []
        entries: list[SessionEntry] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict) and record.get("type") == "message":
                    entries.append(MessageEntry.from_record(record))
                else:
                    entries.append(runtime.entry_from_record(record))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"无效会话记录，第 {line_number} 行：{error}") from error
        return entries


# s15 修改：只增加工具状态的保存与恢复，投影、压缩和统计仍使用已有实现。
class SessionManager(runtime.SessionManager):
    """增加工具状态保存和查询，复用原有上下文投影、压缩及统计。

    输入：本章 JsonlSessionStore、完整工具结果消息和对应元数据。
    输出：追加式记录，以及某个工具最新的 details。
    流程：循环保存结果批次 → 扩展启动时从完整日志读取最新快照。
    边界：不从模型正文或摘要恢复状态，不改变 s11 的模型用量规则。
    """

    # s15 修改：打开本章解析器，继承的 build_context 仍只读取每条 Entry.message。
    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开支持工具元数据的会话，不加载或执行扩展。

        输入：会话 JSONL 路径；输出：绑定本章解析器的 SessionManager。
        流程：创建 JsonlSessionStore → 返回会话组件，读取由后续查询显式触发。
        """
        return cls(JsonlSessionStore(path))

    def append_tool_results(self, message: Message, tool_details: ToolDetails) -> None:
        """把整个工具批次的正文与状态写入同一条记录。

        输入：协议合法的 user/tool_result 消息，及按调用 ID 对应的元数据。
        输出：无；成功后才能发出结果事件，失败由循环停止运行。
        流程：规范化消息 → 检查元数据归属 → 追加一条 JSONL，不另写任务文件。
        """
        # s15 新增：与 s11 的模型用量一样，状态附在记录上而非模型正文中。
        base = runtime.MessageEntry.from_message(message)
        tool_details = _validate_and_copy_tool_details(base.message, tool_details)
        entry = MessageEntry(message=base.message, tool_details=tool_details)
        self.store.append(entry)

    def latest_tool_details(self, tool_name: str) -> dict[str, Any] | None:
        """从完整日志取得指定工具最近的状态快照。

        输入：工具名称；输出：独立 details 字典，没有状态时返回 None。
        流程：反向查找消息 → 反向查找同批工具结果 → 返回匹配名称的快照。
        参考 Pi 重建扩展状态；本项目只处理线性会话，不实现分支或从摘要推断状态。
        """
        # s15 新增：使用 load_entries 而不是 build_context，压缩不能丢掉恢复依据。
        for entry in reversed(self.load_entries()):
            if not isinstance(entry, MessageEntry):
                continue
            # s15 新增：同批可能多次调用同一工具，也从最后一次调用开始查找。
            calls_newest_first = reversed(list(entry.tool_details.values()))
            for saved_call in calls_newest_first:
                if saved_call["name"] == tool_name:
                    return deepcopy(saved_call["details"])
        return None
