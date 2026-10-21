"""s17：沿用 s16 的目标访问边界，仅隔离语义检索章节的缓存与运行记录。"""

from s16_repository_search.workspace import RepositoryWorkspace as SearchWorkspace


# 来自 s16：保持；源码扫描、只读工具、会话归属校验均由基础工作区提供。
class RepositoryWorkspace(SearchWorkspace):
    """绑定 s17 的目标、索引、Session 和 Trace。

    输入：目标和 Harness 路径；输出：存储目录按 s17 隔离的工作区。
    流程：父类读取 chapter → 计算目标身份与目录 → 组装相同的只读工具。
    边界：只更换命名空间，不扩大允许读取或发送给 embedding 的源码范围。
    """

    chapter = "s17"
