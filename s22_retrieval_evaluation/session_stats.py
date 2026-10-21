"""s22：从真实持久记录重算会话成本，不运行 Agent，不猜测最终候选。"""

import json
from dataclasses import asdict
from pathlib import Path

from s11_runtime_observability import code as runtime
from s15_stateful_extensions.session import MessageEntry


def read_session_stats(path: Path) -> dict:
    """只读统计回答、摘要、失败和工具调用，保留未知用量。

    输入：用户明确指定的普通 JSONL；输出：会话级统计及 embedding 成本。
    流程：有界读取 → 复用 Entry 校验 → SessionStats 汇总 → 单列工具 details 编码用量。
    边界：不打开可追加 SessionManager、不写原日志；不能把多任务会话当单任务定位阶段。
    """
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("统计输入必须是已有独立普通会话文件")
    with path.open("rb") as file:
        data = file.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("会话超过 64 MiB")
    entries = []
    embedding = {"requests": 0, "known_tokens": 0, "unknown_usage_requests": 0}
    user_tasks = 0
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            entry = (
                MessageEntry.from_record(record)
                if isinstance(record, dict) and record.get("type") == "message"
                else runtime.entry_from_record(record)
            )
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"会话第 {number} 行损坏，未跳过失败记录") from error
        entries.append(entry)
        if isinstance(entry, MessageEntry):
            if entry.message["role"] == "user" and isinstance(entry.message["content"], str):
                user_tasks += 1
            for details in entry.tool_details.values():
                usage = details["details"].get("embedding", {})
                if not isinstance(usage, dict):
                    raise TypeError("embedding 指标损坏")
                for key in embedding:
                    value = usage.get(key, 0)
                    if type(value) is not int or value < 0:
                        raise ValueError("embedding 指标必须为非负整数")
                    embedding[key] += value
    stats = runtime.SessionStats.from_entries(entries)
    return {
        "layer": "session_observation",
        "scope": "entire_session",
        "user_inputs": user_tasks,
        "chat": asdict(stats),
        "embedding": embedding,
        "notice": "会话统计不是组件基线工具次数；未推断定位阶段边界或 Agent 最终文件排名",
    }
