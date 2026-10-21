#!/usr/bin/env python3
"""s22：评测系统 · 定位消融；无模型组件评测与真实会话成本分开。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from s16_repository_search.index import RepositoryIndex
from s16_repository_search.workspace import checked_cache_path
from s16_repository_search.zoekt import DEFAULT_IMAGE, ZoektBackend
from s17_semantic_retrieval.embeddings import OpenAIEmbeddingProvider
from s17_semantic_retrieval.semantic import SemanticIndex
from s18_relationship_navigation.index import RelationshipIndex
from s18_relationship_navigation.retrieval import RelationshipRetriever
from s19_retrieval_fusion.retrieval import CodeRetriever
from s22_retrieval_evaluation.dataset import load_cases
from s22_retrieval_evaluation.evaluation import EvaluationRunner
from s22_retrieval_evaluation.session_stats import read_session_stats
from s22_retrieval_evaluation.strategies import MODES, ComponentLocator
from s22_retrieval_evaluation.workspace import RepositoryWorkspace
from zero2pi.ui import format_error

HARNESS_ROOT = Path(__file__).resolve().parents[1]


def parse_args(arguments=None):
    """解析一个评测方案或只读会话统计，不偷偷启动模型或下载样本。

    输入：固定目标/JSONL/模式/可选输出；输出：Namespace。
    流程：解析 → 授权与模式检查 → main 组装当前真实使用的组件。
    边界：默认 basic 不要 Docker/API；hybrid 必须显式授权，不能静默退回文本。
    """
    parser = argparse.ArgumentParser(description="s22：定位组件消融与会话成本统计")
    parser.add_argument(
        "--workspace", type=Path, default=HARNESS_ROOT / "example_project" / "retrieval_demo"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=HARNESS_ROOT / "s22_retrieval_evaluation" / "tasks" / "retrieval_demo.jsonl",
    )
    parser.add_argument("--mode", choices=MODES, default="basic")
    parser.add_argument("--zoekt-image", default=DEFAULT_IMAGE)
    parser.add_argument("--allow-remote-embedding", action="store_true")
    parser.add_argument("--output", type=Path, help="目标外的新报告文件；已存在则拒绝覆盖")
    parser.add_argument(
        "--session-stats", type=Path, help="只读已有会话成本，不评分 Agent 最终答案"
    )
    options = parser.parse_args(arguments)
    if (
        options.mode.startswith("hybrid")
        and not options.allow_remote_embedding
        and options.session_stats is None
    ):
        parser.error("hybrid 需要独立 embedding 配置与 --allow-remote-embedding")
    if options.allow_remote_embedding and (
        not options.mode.startswith("hybrid") or options.session_stats
    ):
        parser.error("embedding 授权只用于 hybrid 组件评测")
    if options.session_stats and (options.mode != "basic" or options.output):
        parser.error("会话成本入口不接受组件模式或输出文件；只读结果写 stdout")
    return options


def main(arguments=None) -> None:
    """执行一项可复核评测，不在样本不足时编造对照成绩。

    输入：目标、数据集、策略或实际 Session；输出：JSON 报告和明确退出状态。
    流程：核对标签与版本 → 按模式组装组件 → 盲定位/评分 → 在目标外保存新报告。
    边界：组件入口不调用聊天模型，所以不放无用 agent_loop；真实运行仍用 s20/s21。
    """
    options = parse_args(arguments)
    if options.session_stats:
        print(json.dumps(read_session_stats(options.session_stats), ensure_ascii=False, indent=2))
        return
    workspace = RepositoryWorkspace(options.workspace, HARNESS_ROOT)
    # s22 修改：费用发生前先拒绝目标内路径/旧报告；结束仍用 x 防止并发覆盖。
    output = options.output or HARNESS_ROOT / ".evals" / "s22" / (uuid4().hex + ".json")
    output = output.absolute()
    checked_cache_path(output)
    if output.is_relative_to(workspace.root):
        raise ValueError("评测报告必须保存在目标项目之外")
    if output.exists():
        raise FileExistsError("评测报告已存在，不能覆盖")
    cases = load_cases(options.dataset, workspace=workspace)
    # s22 新增：先检查标签隔离/版本，再启动任何引擎或可能计费的服务。
    workspace.prepare_index_directory()
    retriever = None
    relationship_retriever = None
    if options.mode != "basic":
        zoekt_backend = ZoektBackend(options.zoekt_image)
        zoekt_backend.check_ready()
        index_manager = RepositoryIndex(workspace=workspace, backend=zoekt_backend)
        semantic_index = None
        if options.allow_remote_embedding:
            load_dotenv(HARNESS_ROOT / ".env", override=True)
            embedding_provider = OpenAIEmbeddingProvider.from_env()
            semantic_index = SemanticIndex(
                index_root=workspace.index_root, provider=embedding_provider
            )
            print("评测  已允许向 embedding 服务发送合规源码与查询，可能计费。", file=sys.stderr)
        retriever = CodeRetriever(
            workspace=workspace, index_manager=index_manager, semantic_index=semantic_index
        )
        if options.mode == "hybrid-relations":
            relationship_index = RelationshipIndex(workspace=workspace)
            relationship_retriever = RelationshipRetriever(
                workspace=workspace, relationship_index=relationship_index
            )
    locator = ComponentLocator(
        workspace=workspace,
        mode=options.mode,
        retriever=retriever,
        relationship_retriever=relationship_retriever,
    )
    evaluator = EvaluationRunner(workspace=workspace, locator=locator)
    report = evaluator.run(cases)
    # s22 新增：结果与 gold 都不能写入被检索目标；保存采用 x，不覆盖旧评测。
    # s22 新增：运行中路径可能改变，真正写入前再次核对，不把前检当文件系统锁。
    checked_cache_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    print(f"评测报告：{output}", file=sys.stderr)
    if report["failed_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, TypeError, OSError) as error:
        print(format_error(str(error)), file=sys.stderr)
        raise SystemExit(1) from error
