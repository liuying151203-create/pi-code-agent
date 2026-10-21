"""s20：保持读取边界，只绑定独立的章节存储。"""

from s19_retrieval_fusion.workspace import RepositoryWorkspace as SearchWorkspace


# 来自 s19：保持；chapter 只决定目录，源码访问规则不扩大。
class RepositoryWorkspace(SearchWorkspace):
    """为任务上下文和检索绑定同一目标。

    输入：目标和 Harness 路径；输出：s20 的工作区、记录与索引路径。
    流程：父类规范化路径 → 选择 chapter 命名空间 → 沿用只读工具。
    边界：不初始化目标 Git，不复制旧章会话或授权。
    """

    chapter = "s20"
