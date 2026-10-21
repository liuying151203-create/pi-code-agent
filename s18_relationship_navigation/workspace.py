"""s18：复用 s17 的访问边界，仅隔离本章存储。"""

from s17_semantic_retrieval.workspace import RepositoryWorkspace as SemanticWorkspace


# 来自 s17：保持；类属性只选择目录，不重写权限、扫描或基础工具。
class RepositoryWorkspace(SemanticWorkspace):
    """绑定 s18 的目标、缓存、Session 和 Trace。

    输入：目标与 Harness 路径；输出：s18 工作区。
    流程：父类构造读取 chapter → 计算路径 → 基础工具沿用同一目标。
    边界：不扩大 s17 的源码允许清单，不跨章恢复会话。
    """

    chapter = "s18"
