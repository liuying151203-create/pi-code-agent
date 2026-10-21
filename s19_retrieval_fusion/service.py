"""s19：工具之下的仓库检索服务，统一目标，不把关系能力塞入 Zoekt 后端。"""


# s19 新增：搜索和关系是同一服务的两种操作，仍允许模型直接选择明确的工具入口。
class RepositoryRetrieval:
    """向工具层提供搜索和关系导航两个操作。

    输入：同目标的搜索检索器、关系检索器；输出：两种操作的标准 ToolResult。
    流程：构造时核对 workspace → search 分派多路召回 → related 分派实体关系导航。
    边界：不扫描、不排名、不递归调用模型工具；组件的关键执行流程保留在各自方法中。
    """

    def __init__(self, *, search_retriever, relationship_retriever):
        """保存可见依赖；目标不同立即拒绝，不等到模型调用才发现。"""
        if search_retriever.workspace is not relationship_retriever.workspace:
            raise ValueError("搜索与关系导航必须共用 workspace")
        self.search_retriever = search_retriever
        self.relationship_retriever = relationship_retriever
        self.workspace = search_retriever.workspace
        self.semantic_index = search_retriever.semantic_index
        # s19 新增：自动补充按搜索器的实际能力公布，不能借导航器的配置虚报能力。
        self.relationship_index = search_retriever.relationship_index

    def search(
        self,
        query: str,
        scope: str = "",
        limit: int = 10,
        mode: str = "text",
        keywords: list[str] | None = None,
        include_related: bool = False,
    ):
        """按查询定位代码；需要时由搜索检索器在同次快照内补充关系。"""
        return self.search_retriever.search(
            query,
            scope=scope,
            limit=limit,
            mode=mode,
            keywords=keywords,
            include_related=include_related,
        )

    def related(
        self, entity_id: str, direction: str = "incoming", kind: str = "calls", limit: int = 10
    ):
        """按已知实体直接导航，不要求先调用向量搜索。"""
        return self.relationship_retriever.related(
            entity_id, direction=direction, kind=kind, limit=limit
        )
