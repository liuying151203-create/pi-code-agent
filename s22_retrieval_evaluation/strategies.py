"""s22：固定输入、不同能力的组件对照，不冒充 LLM Agent 行为基线。"""

import json
import re
from dataclasses import dataclass, field

from s19_retrieval_fusion.evidence import fuse_files
from s22_retrieval_evaluation.dataset import SearchTask

MODES = ("basic", "text", "text-symbol", "hybrid", "hybrid-relations")


@dataclass
class RetrievalRun:
    """保存一次定位的有序候选和真实组件调用量，不包含评测标签。"""

    files: list[str] = field(default_factory=list)
    status: str = "ok"
    logical_tool_calls: int = 0
    metrics: list[dict] = field(default_factory=list)
    error: str | None = None


class ComponentLocator:
    """按固定策略运行检索组件，输入不带 gold。

    输入：workspace、策略名、可选检索/关系组件；输出：RetrievalRun。
    流程：basic 调基础工具，其他模式调用已有搜索 → 可选关系导航 → 文件级排名。
    边界：这是 scripted component baseline，不调用聊天模型；工具计数不等于 Agent 实测轮数。
    """

    def __init__(self, *, workspace, mode: str, retriever=None, relationship_retriever=None):
        """拒绝不存在的模式和缺失依赖，不静默把未配置语义切成文本。"""
        if (
            mode not in MODES
            or (mode != "basic" and retriever is None)
            or (mode == "hybrid-relations" and relationship_retriever is None)
        ):
            raise ValueError("评测模式无效或缺少真实组件")
        self.workspace, self.mode = workspace, mode
        self.retriever, self.relationship_retriever = retriever, relationship_retriever

    def locate(self, task: SearchTask) -> RetrievalRun:
        """执行一项盲任务；任何通道故障都保留失败，而不返回伪装的空成功。

        输入：SearchTask；输出：最多 20 个去重文件、状态与各次组件指标。
        流程：选择策略 → 运行有界工具 → 收集候选 → 可选单跳 → 统一去重。
        边界：关系只扩展任务明确提供的起点，不能读取答案文件路径来挑 seed。
        """
        run = RetrievalRun()
        try:
            if self.mode == "basic":
                run.files = self._basic(task, run)
                return run
            if self.mode in {"text", "text-symbol"}:
                text_files = []
                for keyword in task.keywords:
                    result = self._search(run, keyword, mode="text")
                    text_files.extend(file["path"] for file in result["files"])
                channels = [list(dict.fromkeys(text_files))]
                if self.mode == "text-symbol":
                    result = self._search(run, task.symbol_query, mode="symbol")
                    channels.append([file["path"] for file in result["files"]])
                run.files = fuse_files(channels)[:20]
                return run
            hybrid = self._search(run, task.query, mode="hybrid", keywords=list(task.keywords))
            channels = [[file["path"] for file in hybrid["files"]]]
            if self.mode == "hybrid-relations" and task.seed is not None:
                definitions = self._search(run, task.seed["query"], mode="symbol")
                entities = {
                    hit["entity"]["entity_id"]
                    for file in definitions["files"]
                    for hit in file["hits"]
                    if hit["entity"]
                    and hit["entity"]["qualified_name"] == task.seed["qualified_name"]
                }
                if len(entities) != 1:
                    raise ValueError("已知起点未找到或有歧义；不根据 gold 选择实体")
                run.logical_tool_calls += 1
                result = self.relationship_retriever.related(
                    next(iter(entities)), direction="incoming", kind="calls", limit=20
                )
                run.metrics.append(result.details or {})
                related = self._result(result)
                # s22 新增：关系通道不完整也要保留状态，不能把部分召回标成完整成功。
                if related["status"] == "partial":
                    run.status = "partial"
                channels.append(
                    [
                        edge["source"]["path"]
                        for edge in related["relations"]
                        if edge["target_id"] is not None
                    ]
                )
            run.files = fuse_files(channels)[:20]
        except (RuntimeError, ValueError, TypeError, OSError, KeyError) as error:
            run.status = "failed"
            run.files = []
            run.error = str(error)[:700]
        return run

    def _search(self, run, query, **options):
        """一次真实搜索计一次逻辑工具调用，内部扫描/embedding 请求另看 metrics。"""
        run.logical_tool_calls += 1
        result = self.retriever.search(query, limit=20, **options)
        run.metrics.append(result.details or {})
        data = self._result(result)
        if data["status"] == "partial":
            run.status = "partial"
        return data

    def _result(self, result):
        """只解析标准工具 JSON，不从助手自由文本猜测排名。"""
        data = json.loads(result.content.removeprefix("Error: "))
        if not isinstance(data, dict):
            raise TypeError("工具结果不是标准对象")
        if result.content.startswith("Error:") or data.get("status") not in {
            "ok",
            "empty",
            "partial",
        }:
            raise RuntimeError(data.get("error", "检索未成功"))
        return data

    def _basic(self, task, run):
        """固定 glob→逐文件 grep→命中窗口 read，非智能 Agent 基线；省略会标 partial。"""
        run.logical_tool_calls += 1
        listing = self.workspace.glob("*")
        allowed = self.workspace.scan().file_contents
        paths = [line for line in listing.splitlines() if line in allowed]
        if "[路径已截断" in listing:
            run.status = "partial"
        hits = []
        for path in paths:
            first_line = None
            for keyword in task.keywords:
                run.logical_tool_calls += 1
                matches = self.workspace.grep(path, keyword, limit=1)
                matched = re.search(re.escape(path) + r":(\d+):", matches)
                if matched:
                    first_line = int(matched.group(1))
                    break
            if first_line is not None:
                run.logical_tool_calls += 1
                self.workspace.read_file(path, start_line=first_line, limit=5)
                hits.append(path)
        return hits[:20]
