"""s16 新增：已有 ExtensionAPI 注册检索工具和指标观察者。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from s11_runtime_observability.code import AgentEvent
from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.result import ToolResult
from s16_repository_search.retrieval import CodeRetriever
from s16_repository_search.workspace import checked_cache_path

# ===== s16 新增：工具定义与扩展注册 =====
# s16 新增：模型只提交字面查询和有界范围，不能调用宿主 shell 或任意后端参数。
SEARCH_TOOL = {
    "name": "search_code",
    "description": (
        "在目标项目中用 Zoekt 搜索区分大小写的字面文本，返回排序文件、行号和内容哈希。"
        "用单个标识符或错误文本定位；scope 可指定相对目录；不是自然语言语义搜索。"
        "返回最多 20 个文件 / 16 KiB；需要更多源码时用 read_file 并传 expected_hash。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "scope": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


# s16 新增：参考 Pi 的工具/事件注册，检索依赖通过构造参数传入，不改模块加载器。
class RepositoryExtension:
    """把检索接到现有工具分发，并将保存后的指标写入独立 JSONL。

    输入：retriever 与本次运行的 metrics_path；输出：一个工具和一个事件订阅。
    流程：main 显式 register → handle_search 接收工具参数 → 检索返回正文/details
    → 会话保存 → 事件写指标。
    边界：不恢复索引状态，不改 Todo，不将完整源码、候选和查询正文重复写入指标。
    """

    def __init__(self, *, retriever: CodeRetriever, metrics_path: Path) -> None:
        """保存显式依赖，不创建文件或触发搜索。"""
        self.retriever = retriever
        self.metrics_path = checked_cache_path(metrics_path)

    def register(self, api: ExtensionAPI) -> None:
        """向已有 API 提供 handler 和观察者，不需要新增循环参数。

        输入：本次 ExtensionAPI；输出：更新工具目录与 tool_result 订阅。
        流程：register_tool 绑定 handle_search → on 绑定 record_metrics；注册时不执行工具。
        """
        api.register_tool(SEARCH_TOOL, self.handle_search)
        api.on("tool_result", self.record_metrics)

    # s16 新增：用 handle_search 表示工具适配入口，避免与实际检索流程同名。
    def handle_search(self, arguments: dict[str, Any]) -> ToolResult:
        """接收工具参数并交给检索器，不在扩展层实现搜索。

        输入：模型给出的 query、scope、limit 字典；输出：检索器的 ToolResult。
        流程：拒绝未知字段或缺少 query → 将合法参数传给 CodeRetriever.search。
        """
        if set(arguments) - {"query", "scope", "limit"} or "query" not in arguments:
            # s16 新增：入口拒绝也附失败指标，不能在统计里漏掉参数错误调用。
            payload = {
                "status": "invalid",
                "mode": "text",
                "files": [],
                "truncated": False,
                "search_exhaustive": False,
                "error": "search_code 只接受 query、scope、limit，且 query 必填",
            }
            return ToolResult(
                "Error: " + json.dumps(payload, ensure_ascii=False),
                {
                    "kind": "retrieval",
                    "workspace_id": self.retriever.workspace.workspace_id,
                    "mode": "text",
                    "status": "invalid",
                    "returned_files": 0,
                    "truncated": False,
                    "search_exhaustive": False,
                },
            )
        return self.retriever.search(**arguments)

    def record_metrics(self, event: AgentEvent) -> None:
        """记录保存成功后的检索指标，不另建 Event 总线。

        输入：tool_result 事件；输出：追加一行指标。
        流程：只选择 search_code 的 retrieval details → 补调用 ID → 写独立 JSONL。
        边界：现有 Trace 不保留完整 details，因此单独写；写失败沿用 s11 的停止策略。
        """
        details = event.data.get("details")
        if (
            event.data.get("name") != "search_code"
            or not isinstance(details, dict)
            or details.get("kind") != "retrieval"
        ):
            return
        checked_cache_path(self.metrics_path)
        if self.metrics_path.exists() and self.metrics_path.stat().st_nlink > 1:
            raise ValueError("指标文件不能是硬链接")
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"tool_call_id": event.data.get("tool_call_id"), **details}
        with self.metrics_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
