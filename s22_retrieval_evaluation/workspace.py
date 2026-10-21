"""s22：沿用目标读取边界，只隔离评测使用的索引缓存。"""

from s20_git_task_context.workspace import RepositoryWorkspace as TaskWorkspace


class RepositoryWorkspace(TaskWorkspace):
    """将组件评测绑定为 s22，不从评测标签恢复权限。

    输入：目标/Harness；输出：s22 工作区路径与只读工具。
    流程：父类构造 → chapter 选择缓存目录 → 复用实际扫描/读取规则。
    边界：标签和结果不在目标内；不创建 Agent 或空的核心循环。
    """

    chapter = "s22"
