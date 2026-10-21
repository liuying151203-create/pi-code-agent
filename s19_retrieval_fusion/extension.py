"""s19：统一服务的工具适配；排名和图分析不进入扩展注册层。"""

import uuid
from copy import deepcopy
from typing import Any

from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.result import ToolResult
from s16_repository_search.extension import SEARCH_TOOL as SYMBOL_TOOL
from s17_semantic_retrieval.extension import RepositoryExtension as SemanticExtension
from s17_semantic_retrieval.extension import write_metrics
from s18_relationship_navigation.extension import RELATED_TOOL


# s19 修改：复用 s17 的指标写入；工具目录只公布当前真正启用的模式。
class SearchExtension(SemanticExtension):
    """将多路检索接入已有工具注册表。

    输入：CodeRetriever 和 metrics_path（沿用父类构造）；输出：工具定义及事件订阅。
    流程：根据 semantic_index 的实际配置生成目录 → handler 交给 search → 原观察者记录。
    边界：不通过系统提示词代替配置/授权检查；不新增事件总线。
    """

    def register(self, api: ExtensionAPI) -> None:
        """按实际依赖公布能力；未启用 embedding 时只有 text/symbol。"""
        tool = deepcopy(SYMBOL_TOOL)
        if self.retriever.semantic_index is not None:
            tool["input_schema"]["properties"]["mode"]["enum"] += ["semantic", "hybrid"]
            tool["input_schema"]["properties"]["keywords"] = {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
            }
            tool["description"] = (
                "搜索当前项目。text 搜字面文本，symbol 搜定义，semantic 用自然语言搜含义；"
                "hybrid 同时使用语义、文本和符号：query 描述需求，keywords 必须给 1–4 个代码关键词。"
                "返回有界文件、通道名次、哈希和实体。用 read_file 按范围补读，程序自动校验指纹。"
            )
        # s19 新增：仅实际组装关系索引时才公布自动补充参数。
        if self.retriever.relationship_index is not None:
            tool["input_schema"]["properties"]["include_related"] = {
                "type": "boolean",
                "default": False,
                "description": "为最多三个主要实体补充同范围内的直接调用者，不参与 RRF",
            }
        api.register_tool(tool, self.handle_search)
        api.on("tool_result", self.record_metrics)

    def handle_search(self, arguments: dict[str, Any]) -> ToolResult:
        """校验字段后显式传给检索器，不替模型猜测关键词或静默切换通道。"""
        if (
            set(arguments) - {"query", "scope", "limit", "mode", "keywords", "include_related"}
            or "query" not in arguments
        ):
            return ToolResult(
                "Error: search_code 只接受 query/scope/limit/mode/keywords/include_related，query 必填",
                {
                    "kind": "retrieval",
                    "status": "invalid",
                    "query_id": uuid.uuid4().hex,
                    "workspace_id": self.retriever.workspace.workspace_id,
                    "mode": "invalid",
                },
            )
        return self.retriever.search(**arguments)


# s19 新增：两个工具都适配同一个 RepositoryRetrieval；内部能力分层不等于增加模型调用步骤。
class RepositoryExtension(SearchExtension):
    """注册统一仓库服务的搜索和关系入口，复用已有工具事件记录。

    输入：RepositoryRetrieval 和 metrics_path；输出：search_code、related_code 及指标订阅。
    流程：注册搜索入口 → 注册关系入口 → 事件分别按真实工具名称记录。
    边界：工具层只校验字段和转发参数，不执行融合或图分析。
    """

    def register(self, api: ExtensionAPI) -> None:
        """向已有 API 提供两个独立入口，不新增 Agent loop 分支。"""
        super().register(api)
        api.register_tool(RELATED_TOOL, self.handle_related)
        api.on("tool_result", self.record_relation_metrics)

    def handle_related(self, arguments: dict[str, Any]) -> ToolResult:
        """按实体导航；拒绝未知字段，避免把任意参数传入图查询。"""
        if (
            set(arguments) - {"entity_id", "direction", "kind", "limit"}
            or "entity_id" not in arguments
        ):
            return ToolResult(
                "Error: related_code 只接受 entity_id/direction/kind/limit",
                {
                    "kind": "retrieval",
                    "status": "invalid",
                    "workspace_id": self.retriever.workspace.workspace_id,
                },
            )
        return self.retriever.related(**arguments)

    def record_relation_metrics(self, event) -> None:
        """关系统计使用真实工具名，不伪装成 search_code 事件。"""
        write_metrics(event, self.metrics_path, tool_name="related_code")
