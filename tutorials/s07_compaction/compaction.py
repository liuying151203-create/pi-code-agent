"""s07：计算安全切点并请求摘要，会话持久化由 AgentSession 负责。"""

from __future__ import annotations

import json
import math
from contextlib import aclosing
from dataclasses import dataclass

from .messages import AssistantMessage, Text, Thinking, ToolCall, Usage, UserMessage


# ===== s07 新增 =====
# s07 新增：记录安全切点与被切开的回合。
@dataclass(frozen=True)
class CutPoint:
    """保存近期记录的起点；split_turn_start 指向被切开的用户回合。"""

    first_kept: int
    split_turn_start: int | None


# s07 新增：向 SDK 返回已经保存的压缩结果。
@dataclass(frozen=True)
class CompactionResult:
    """返回已写入的摘要、保留起点及摘要请求的实际用量。"""

    summary: str
    first_kept_entry_id: str
    tokens_before: int | None
    usage: Usage | None


# s07 新增：单独请求模型整理历史，不提供业务工具。
async def summarize(model, messages, previous_summary, instructions, max_tokens, encode_message):
    """使用同一模型生成摘要，不提供业务工具，也不修改会话历史。"""
    transcript = [encode_message(message) for message in messages]
    # s07 调整：参考 Pi 的摘要序列化，保留可读推理但不发送签名或脱敏数据。
    for item in transcript:
        if item["role"] == "assistant":
            item.pop("usage", None)
            item["content"] = [
                {"type": "thinking", "text": block["text"]}
                if block["type"] == "thinking"
                else block
                for block in item["content"]
                if block["type"] != "thinking" or not block["redacted"]
            ]
        elif item["role"] == "tool_result" and len(item["content"]) > 2000:
            omitted = len(item["content"]) - 2000
            item["content"] = item["content"][:2000] + f"\n\n[省略 {omitted} 个字符]"
    prompt = (
        "请将以下历史整理为可继续工作的摘要，保留目标、约束、进度、关键决定、"
        "下一步以及必要的路径和错误信息。不要执行工具。\n"
        + ("上次摘要：\n" + previous_summary + "\n" if previous_summary else "")
        + ("用户的额外要求：\n" + instructions + "\n" if instructions else "")
        + "历史消息：\n"
        + json.dumps(transcript, ensure_ascii=False)
    )
    arguments = {
        "system": "你负责整理对话摘要，不执行任务。",
        "messages": (UserMessage(prompt),),
        "tools": (),
        "max_tokens": max_tokens,
    }
    if callable(getattr(model, "complete", None)):
        response = await model.complete(**arguments)
    else:
        response = None
        async with aclosing(model.stream(**arguments)) as stream:
            async for event in stream:
                if event["type"] == "done":
                    if response is not None:
                        raise RuntimeError("摘要流重复结束")
                    response = event["message"]
    if not isinstance(response, AssistantMessage) or response.stop_reason != "stop":
        raise RuntimeError("摘要请求未正常结束，压缩未保存")
    if any(isinstance(block, ToolCall) for block in response.content):
        raise RuntimeError("摘要请求返回了工具调用，压缩未保存")
    summary = "\n".join(block.text for block in response.content if isinstance(block, Text)).strip()
    if not summary:
        raise RuntimeError("摘要为空，压缩未保存")
    return summary, response.usage


# s07 新增：按 Pi 的字符估算思路选择近期预算。
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


# s07 新增：不以工具结果作为切点，并识别单个超长回合。
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
