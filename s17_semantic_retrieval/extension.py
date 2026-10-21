"""s17：扩展只适配工具参数和已有事件，检索算法留在检索组件。"""

import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.result import ToolResult
from s16_repository_search.extension import SEARCH_TOOL as SYMBOL_TOOL
from s16_repository_search.extension import RepositoryExtension as SymbolExtension
from s16_repository_search.workspace import checked_cache_path


# s17 新增：两个检索工具共享底层写入规则，不伪装事件名称或新建总线。
def write_metrics(event, metrics_path: Path, *, tool_name: str) -> None:
    """只记录指定工具的紧凑 details；事件名称保持真实，不存完整候选正文。"""
    details = event.data.get("details")
    if (
        event.data.get("name") != tool_name
        or not isinstance(details, dict)
        or details.get("kind") != "retrieval"
    ):
        return
    checked_cache_path(metrics_path)
    if metrics_path.exists() and (not metrics_path.is_file() or metrics_path.stat().st_nlink != 1):
        raise ValueError("指标必须写入独立普通文件")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    record = {**details, "tool_call_id": event.data.get("tool_call_id"), "tool_name": tool_name}
    with metrics_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


# s17 修改：保留 s16 的指标观察者；工具目录只公布当前真正启用的模式。
class RepositoryExtension(SymbolExtension):
    """将单路语义检索接入已有工具注册表。

    输入：CodeRetriever 和 metrics_path（沿用父类构造）；输出：工具定义及事件订阅。
    流程：根据 semantic_index 的实际配置生成目录 → handler 交给 search → 原观察者记录。
    边界：不通过系统提示词代替配置/授权检查；不新增事件总线。
    """

    def register(self, api: ExtensionAPI) -> None:
        """按实际依赖公布能力；未启用 embedding 时只有 text/symbol。"""
        tool = deepcopy(SYMBOL_TOOL)
        if self.retriever.semantic_index is not None:
            tool["input_schema"]["properties"]["mode"]["enum"] += ["semantic"]
            tool["description"] = (
                "搜索当前项目。text 搜字面文本，symbol 搜定义，semantic 用自然语言搜含义；"
                "返回有界文件、通道名次、哈希和实体。用 read_file 按范围补读，程序自动校验指纹。"
            )
        api.register_tool(tool, self.handle_search)
        api.on("tool_result", self.record_metrics)

    def handle_search(self, arguments: dict[str, Any]) -> ToolResult:
        """校验字段后显式传给检索器，不替模型猜测关键词或静默切换通道。"""
        if set(arguments) - {"query", "scope", "limit", "mode"} or "query" not in arguments:
            return ToolResult(
                "Error: search_code 只接受 query/scope/limit/mode，query 必填",
                {
                    "kind": "retrieval",
                    "status": "invalid",
                    "query_id": uuid.uuid4().hex,
                    "workspace_id": self.retriever.workspace.workspace_id,
                    "mode": "invalid",
                },
            )
        return self.retriever.search(**arguments)

    def record_metrics(self, event) -> None:
        """沿用 s16 的保存后观察时机，用共用写入器记录真实 search_code 指标。"""
        write_metrics(event, self.metrics_path, tool_name="search_code")
