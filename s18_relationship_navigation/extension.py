"""s18：自然语言选择 search_code 定位，再选择 related_code 导航。"""

import uuid
from typing import Any

from s15_stateful_extensions.registry import ExtensionAPI
from s15_stateful_extensions.result import ToolResult
from s17_semantic_retrieval.extension import RepositoryExtension as SemanticExtension
from s17_semantic_retrieval.extension import write_metrics
from s18_relationship_navigation.retrieval import RelationshipRetriever

RELATED_TOOL = {
    "name": "related_code",
    "description": (
        "用 search_code 返回的 entity.entity_id 查询当前版本的一跳 Python 关系。"
        "incoming/calls 找调用者；outgoing/calls 找被调用者；imports 为导入，inherits 为继承。"
        "返回证据位置和双方实体，最多 20 条/16 KiB；target=null 是无法唯一解析，不是不存在调用。"
        "源码修改后旧 ID 失效，应重新搜索。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "direction": {
                "type": "string",
                "enum": ["incoming", "outgoing"],
                "default": "incoming",
            },
            "kind": {
                "type": "string",
                "enum": ["calls", "imports", "inherits", "all"],
                "default": "calls",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["entity_id"],
        "additionalProperties": False,
    },
}


# s18 新增：两个真实组件并列注入；关系不作为 s17 检索器里隐藏的回调。
class RepositoryExtension:
    """在已有注册表上组合召回和关系导航。

    输入：s17 召回扩展、RelationshipRetriever；输出：两个工具及对应指标观察者。
    流程：register 复用搜索注册 → 注册关系 handler → 订阅保存后的关系指标。
    边界：复用 Event/ToolResult，不另建任务或排名系统；一跳扩展需显式调用。
    """

    def __init__(
        self, *, search_extension: SemanticExtension, relationship_retriever: RelationshipRetriever
    ) -> None:
        """保持依赖可见，并检查两个工具指向同一 workspace。"""
        if search_extension.retriever.workspace is not relationship_retriever.workspace:
            raise ValueError("两个工具必须使用同一工作区")
        self.search_extension = search_extension
        self.relationship_retriever = relationship_retriever

    def register(self, api: ExtensionAPI) -> None:
        """先注册原搜索工具，再追加关系工具，不改已有 handler 或核心循环。"""
        self.search_extension.register(api)
        api.register_tool(RELATED_TOOL, self.handle_related)
        api.on("tool_result", self.record_metrics)

    def handle_related(self, arguments: dict[str, Any]) -> ToolResult:
        """只负责参数适配，关系分析交给 RelationshipRetriever。"""
        if (
            set(arguments) - {"entity_id", "direction", "kind", "limit"}
            or "entity_id" not in arguments
        ):
            return ToolResult(
                "Error: related_code 只接受 entity_id/direction/kind/limit，entity_id 必填",
                {
                    "kind": "retrieval",
                    "status": "invalid",
                    "mode": "relations",
                    "query_id": uuid.uuid4().hex,
                    "workspace_id": self.relationship_retriever.workspace.workspace_id,
                },
            )
        return self.relationship_retriever.related(**arguments)

    def record_metrics(self, event) -> None:
        """复用同一安全指标写入器；只适配事件名，不伪装成一次搜索调用。"""
        write_metrics(event, self.search_extension.metrics_path, tool_name="related_code")
