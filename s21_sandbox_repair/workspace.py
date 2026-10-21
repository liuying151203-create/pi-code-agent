"""s21：宿主布局只管缓存/记录与交付目标；运行时源码来自容器视图。"""

from s16_repository_search.workspace import checked_cache_path
from s20_git_task_context.workspace import RepositoryWorkspace as TaskWorkspace


class RepositoryWorkspace(TaskWorkspace):
    """为副本维护任务绑定隔离存储。

    输入：实际目标和 Harness；输出：s21 记录、目标缓存与宿主报告路径。
    流程：沿用路径身份 → 选择 s21 目录 → 检查报告目录未重定向。
    边界：仅用于最初复制和 Git 元数据，运行时不能拿它替代容器视图。
    """

    chapter = "s21"

    def __init__(self, root, harness_root):
        """初始化布局，再绑定 Harness 内副本报告，不在目标放运行日志。"""
        super().__init__(root, harness_root)
        self.sandbox_root = checked_cache_path(
            harness_root.resolve() / ".sandboxes" / self.chapter / self.workspace_id
        )
