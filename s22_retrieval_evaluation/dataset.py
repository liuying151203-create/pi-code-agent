"""s22：标签留在评测器，检索组件只接收不含答案的任务输入。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchTask:
    """承载各方案共同可见的查询，不包含 gold。

    输入：ID、自然语言、代码关键词、符号查询和可选已知起点。
    输出：交给 ComponentLocator；seed 仅描述用户已知实体，不是答案文件路径。
    流程：load_cases 拆分任务/标签 → locator 只接收本对象。
    """

    task_id: str
    query: str
    keywords: tuple[str, ...]
    symbol_query: str
    seed: dict | None


@dataclass(frozen=True)
class EvaluationCase:
    """评测器持有源码版本和非空标签；不能直接传给模型或检索组件。"""

    task: SearchTask
    source_fingerprint: str
    gold_files: frozenset[str]


def load_cases(path: Path, *, workspace) -> list[EvaluationCase]:
    """先核对标签隔离与源码版本，再构造无答案的任务。

    输入：目标外 JSONL 与明确 workspace；输出：最多 100 个固定版本案例。
    流程：普通文件/大小/位置 → 严格字段校验 → 非空标签与允许文件核对 → 拆分数据。
    边界：不下载数据；重复任务、空标签、新建文件、版本不符均拒绝，不按召回结果筛样本。
    """
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_nlink != 1
        or path.resolve().is_relative_to(workspace.root)
    ):
        raise ValueError("评测标签必须是目标项目之外的独立普通文件")
    with path.open("rb") as file:
        data = file.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        raise ValueError("样本文件超过 256 KiB")
    snapshot = workspace.scan()
    cases = []
    seen = set()
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        required = {"id", "query", "keywords", "symbol_query", "source_fingerprint", "gold_files"}
        if (
            not isinstance(row, dict)
            or not required <= row.keys()
            or row.keys() - required - {"seed"}
        ):
            raise ValueError("样本字段缺失或未知")
        task_id = row["id"]
        if (
            not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id)
            or task_id in seen
        ):
            raise ValueError("任务 ID 非法或重复")
        if row["source_fingerprint"] != snapshot.fingerprint:
            raise ValueError("样本源码指纹不符；先人工核对版本，不自动更新标签")
        for name in ("query", "symbol_query"):
            if (
                not isinstance(row[name], str)
                or not row[name].strip()
                or len(row[name]) > 256
                or any(char in row[name] for char in "\0\r\n")
            ):
                raise ValueError("查询必须为非空有界单行文本")
        words = row["keywords"]
        if (
            not isinstance(words, list)
            or not 1 <= len(words) <= 4
            or any(
                not isinstance(word, str)
                or not word.strip()
                or len(word) > 128
                or any(char in word for char in "\0\r\n")
                for word in words
            )
        ):
            raise ValueError("keywords 必须为 1–4 个有界文本")
        gold = row["gold_files"]
        if (
            not isinstance(gold, list)
            or not 1 <= len(gold) <= 20
            or any(not isinstance(name, str) or name not in snapshot.file_contents for name in gold)
            or len(set(gold)) != len(gold)
        ):
            raise ValueError("gold_files 必须非空、去重且全部属于允许源码；新增文件另行评测")
        seed = row.get("seed")
        if seed is not None and (
            not isinstance(seed, dict)
            or set(seed) != {"query", "qualified_name"}
            or any(
                not isinstance(value, str) or not value or len(value) > 256
                for value in seed.values()
            )
        ):
            raise ValueError("seed 只接受用户已知的 query 和 qualified_name，不接收答案路径")
        cases.append(
            EvaluationCase(
                SearchTask(task_id, row["query"], tuple(words), row["symbol_query"], seed),
                snapshot.fingerprint,
                frozenset(gold),
            )
        )
        seen.add(task_id)
        if len(cases) > 100:
            raise ValueError("一次最多 100 个案例")
    if not cases:
        raise ValueError("评测样本不能为空")
    return cases
