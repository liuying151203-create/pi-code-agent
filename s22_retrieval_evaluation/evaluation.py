"""s22：按预定分母评分，失败/空结果不消失；成本与模型行为分开解释。"""

from time import perf_counter

from s22_retrieval_evaluation.dataset import EvaluationCase


def file_metrics(predicted: list[str], gold: frozenset[str]) -> dict:
    """按去重文件算 Recall/Hit/MRR；空标签是无效样本，不当作满分。"""
    if not gold:
        raise ValueError("gold 必须非空")
    ranked = list(dict.fromkeys(predicted))[:10]
    positions = [index for index, path in enumerate(ranked, 1) if path in gold]
    return {
        "file_recall_at_10": len(set(ranked) & gold) / len(gold),
        "hit_at_10": int(bool(positions)),
        "reciprocal_rank_at_10": 1 / min(positions) if positions else 0.0,
    }


class EvaluationRunner:
    """隔离标签和检索策略，并保留所有预先选定的案例。

    输入：workspace、locator 和 EvaluationCase 列表；输出：逐项证据/指标和宏平均。
    流程：逐项复核固定版本 → 盲检索 → 再复核 → 评分（失败为零）→ 汇总。
    边界：不将组件 Recall 冒充 Agent 修复成功率，不根据召回结果更改样本或标签。
    """

    def __init__(self, *, workspace, locator):
        """评分器持有标签，locator 只拿 SearchTask，不共享完整 case。"""
        if locator.workspace is not workspace:
            raise ValueError("评测器与定位器必须共用 workspace")
        self.workspace, self.locator = workspace, locator

    def run(self, cases: list[EvaluationCase]) -> dict:
        """对预先选定的案例评分，输出逐项结果与固定分母宏平均。

        输入：目标外标签加载得到的案例；输出：可保存 JSON，包含失败与实际调用/耗时。
        流程：每项复核版本 → 仅传盲任务给 locator → 复核候选 → 用 gold 评分 → 汇总。
        边界：失败清空候选并记零分，不删除样本；embedding 原始统计与聊天用量分开。
        """
        if not cases:
            raise ValueError("评测案例不能为空")
        results = []
        for case in cases:
            started = perf_counter()
            record = {
                "task_id": case.task.task_id,
                "status": "failed",
                "predicted_files": [],
                "gold_files": sorted(case.gold_files),
                "logical_tool_calls": 0,
                "retrieval_metrics": [],
            }
            try:
                if self.workspace.compute_fingerprint() != case.source_fingerprint:
                    raise ValueError("源码版本不符，保留失败样本，不自动重建标签")
                located = self.locator.locate(case.task)
                record.update(
                    status=located.status,
                    predicted_files=located.files,
                    logical_tool_calls=located.logical_tool_calls,
                    retrieval_metrics=located.metrics,
                )
                if located.error:
                    record["error"] = located.error
                if self.workspace.compute_fingerprint() != case.source_fingerprint:
                    raise ValueError("定位期间源码变化，清空过期候选")
                allowed = self.workspace.scan().file_contents
                if any(path not in allowed for path in record["predicted_files"]):
                    raise ValueError("定位器返回允许清单之外的文件")
                if record["status"] not in {"ok", "partial"}:
                    record["predicted_files"] = []
            except (RuntimeError, ValueError, TypeError, OSError) as error:
                record.update(status="failed", predicted_files=[], error=str(error)[:700])
            record.update(file_metrics(record["predicted_files"], case.gold_files))
            record["duration_ms"] = (perf_counter() - started) * 1000
            results.append(record)
        averages = {
            name: sum(item[name] for item in results) / len(results)
            for name in (
                "file_recall_at_10",
                "hit_at_10",
                "reciprocal_rank_at_10",
                "logical_tool_calls",
                "duration_ms",
            )
        }
        return {
            "layer": "component",
            "mode": self.locator.mode,
            "scripted_baseline": True,
            "case_count": len(results),
            "failed_cases": sum(item["status"] == "failed" for item in results),
            "averages": averages,
            "cases": results,
            "notice": "不是 Agent 性能结论；逻辑工具调用不等于模型轮数，embedding 成本单列",
        }
