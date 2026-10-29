"""s07：计算历史压缩的安全切点，不负责模型请求或会话落盘。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from pi_code_agent.messages import AssistantMessage, Text, Thinking, ToolCall, UserMessage


@dataclass(frozen=True)
class CutPoint:
    """保存近期记录的起点；split_turn_start 指向被切开的用户回合。"""

    first_kept: int
    split_turn_start: int | None


def estimate_message_tokens(message) -> int:
    """估算单条消息大小：文本字符数除以四；不表示供应商实际计费。"""
    if isinstance(message, AssistantMessage):
        chars = 0
        for block in message.content:
            if isinstance(block, (Text, Thinking)):
                chars += len(block.text)
            elif isinstance(block, ToolCall):
                chars += len(block.name) + len(json.dumps(block.arguments, ensure_ascii=False))
    else:
        chars = len(message.content)
    return math.ceil(chars / 4)


def find_cut_point(entries: list[dict], keep_recent_tokens: int) -> CutPoint | None:
    """按近期预算反向查找可切分消息，输出索引及被切开的回合起点。

    输入：当前活动的已解码消息条目，条目中 message 保存真实消息。
    流程：反向累计估算 → 找到预算边界之后的用户或模型消息 → 判断是否切开回合。
    边界：不能从工具结果开始；没有可压缩内容时返回 None。
    """
    if type(keep_recent_tokens) is not int or keep_recent_tokens < 1:
        raise ValueError("近期保留预算必须是正整数")
    candidates = [
        index
        for index, entry in enumerate(entries)
        if entry["message"].role in {"user", "assistant", "custom"}
    ]
    if not candidates:
        return None
    cut = candidates[0]
    total = 0
    for index in range(len(entries) - 1, -1, -1):
        total += estimate_message_tokens(entries[index]["message"])
        if total >= keep_recent_tokens:
            cut = next((point for point in candidates if point >= index), candidates[0])
            break
    if cut == 0:
        return None
    message = entries[cut]["message"]
    turn_start = None
    if isinstance(message, AssistantMessage):
        turn_start = next(
            (
                index
                for index in range(cut - 1, -1, -1)
                if isinstance(entries[index]["message"], UserMessage)
                or entries[index]["message"].role == "custom"
            ),
            None,
        )
    return CutPoint(cut, turn_start)
