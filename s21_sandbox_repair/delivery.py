"""s21：在 s13-A 的确认回写上增加“当前版本已经验证”的门槛。"""

from s13_a_sandbox_copy.workspace_sync import WorkspaceSync as CopySync
from s13_a_sandbox_copy.workspace_sync import WorkspaceSyncError
from s21_sandbox_repair.view import snapshot_fingerprint


# s21 新增：私有预览状态只在派生组件内访问，修复编排器不耦合父类字段。
class VerifiedDelivery(CopySync):
    """保留冲突/备份/确认边界，并拒绝未经验证的版本。

    输入：s13-A 原有依赖与最近验证指纹；输出：待确认 Diff 和验证后的交付。
    流程：preview → mark_verified → 用户确认 → apply；新任务先 invalidate。
    边界：验证成功不自动写回；预览或副本变化后不能沿用旧的测试通过结论。
    """

    verified_fingerprint: str | None = None

    @property
    def preview_fingerprint(self) -> str:
        """提供预览版本，不让其他组件读取 _pending 或解析 Diff 推断版本。"""
        if self._pending is None:
            raise WorkspaceSyncError("尚未预览当前副本")
        return snapshot_fingerprint(self._pending)

    def invalidate(self) -> None:
        """新任务/失败撤销旧结论，避免一次通过批准之后的任务。"""
        self.verified_fingerprint = None

    def mark_verified(self, fingerprint: str) -> None:
        """只标记与预览完全一致的被测版本，不接受模型给出的成功字符串。"""
        if fingerprint != self.preview_fingerprint:
            raise WorkspaceSyncError("预览版本不等于被验证版本")
        self.verified_fingerprint = fingerprint

    def apply(self) -> int:
        """用户确认后仍核对验证版本，再由 s13-A 检查副本变化和宿主冲突。"""
        if (
            self.verified_fingerprint is None
            or self.verified_fingerprint != self.preview_fingerprint
        ):
            raise WorkspaceSyncError("当前预览没有验证通过，不能应用；请先完成修复任务")
        count = super().apply()
        self.invalidate()
        return count
