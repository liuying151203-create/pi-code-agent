"""s16 新增：显式组织扫描、索引、Zoekt 查询和有界源码证据。"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any

from s15_stateful_extensions.result import ToolResult
from s16_repository_search.index import RepositoryIndex
from s16_repository_search.workspace import RepositoryWorkspace, SourceSnapshot, source_lines

# ===== s16 新增：检索流程与证据组织 =====
# s16 新增：检索正文有独立预算，不修改已有会话压缩算法来掩盖输出问题。
MAX_RESULT_BYTES = 16 * 1024
MAX_SNIPPET_CHARS = 500


# s16 新增：检索编排独立于 agent_loop；模型只看到一个 search_code 工具。
class CodeRetriever:
    """将索引匹配转换成可复核、带版本的代码定位证据。

    输入：workspace、index_manager，以及字面查询、目录范围和文件数量。
    输出：ToolResult；正文为有界 JSON，details 只携带紧凑检索指标。
    流程：校验 → 扫描 → 确保索引 → 搜索 → 组织片段 → 复核源码 → 返回。
    边界：不回答代码问题、不生成向量、不扩展调用图、不把后端故障变成空结果。
    """

    def __init__(self, *, workspace: RepositoryWorkspace, index_manager: RepositoryIndex) -> None:
        """保存同源组件；拒绝检索与索引绑定不同目标的错误组装。"""
        if index_manager.workspace is not workspace:
            raise ValueError("retriever 和 index_manager 必须共用同一 workspace")
        self.workspace = workspace
        self.index_manager = index_manager

    def search(self, query: str, scope: str = "", limit: int = 10) -> ToolResult:
        """返回排序文件清单，而不是让模型遍历整个项目。

        输入：区分大小写的字面文本、相对目录和 1–20 个文件的上限。
        输出：ok/empty/partial/stale/failed/invalid 状态，以及哈希、行号和指标。
        流程：扫描源码 → 准备索引 → 查询位置 → 组装文件结果 → 检查源码变化 → 返回。
        source_snapshot 始终表示查询所用的内存源码；prepared_index 只提供磁盘索引位置。
        latest_snapshot 仅用于结束前检查变化；不会拿新版源码解释旧索引的位置。
        """
        request_started_at = perf_counter()
        # s16 新增：模型结果与观测指标分开命名，避免把两者都理解为搜索命中。
        search_metrics: dict[str, Any] = {
            "kind": "retrieval",
            "query_id": uuid.uuid4().hex,
            "workspace_id": self.workspace.workspace_id,
            "mode": "text",
            # s16 新增：未完成阶段用 null，不把失败阶段未测得的耗时写成零。
            "scan_ms": None,
            "index_ms": None,
            "search_ms": None,
            "verify_ms": None,
        }
        search_result: dict[str, Any] = {
            "status": "invalid",
            "mode": "text",
            "files": [],
            "truncated": False,
            "search_exhaustive": False,
        }
        try:
            self._validate(query, scope, limit)
            # s16 新增：参数已合法；此后扫描/更新错误属于运行失败，不误报为参数错误。
            search_result["status"] = "failed"

            # s16 新增：1. 扫描源码，得到内存中的完整字节、文件哈希和整体指纹。
            stage_started = perf_counter()
            source_snapshot = self.workspace.scan()
            search_metrics["scan_ms"] = (perf_counter() - stage_started) * 1000
            search_result["indexed_files"] = len(source_snapshot.file_contents)
            search_result["skipped"] = source_snapshot.skipped_counts

            # s16 新增：2. 准备同版索引；未变化则复用，变化则写过滤副本并交给 Zoekt 重建。
            stage_started = perf_counter()
            prepared_index = self.index_manager.prepare(source_snapshot)
            search_metrics["index_ms"] = (perf_counter() - stage_started) * 1000
            search_result["index_generation"] = prepared_index.generation
            search_metrics["rebuilt"] = prepared_index.rebuilt
            search_metrics["generation"] = prepared_index.generation

            # s16 新增：3. 查询磁盘索引；Zoekt 返回排序后的文件/行号，不返回源码快照。
            stage_started = perf_counter()
            zoekt_result = {"files": [], "exhaustive": True, "truncated": False}
            if source_snapshot.file_contents:
                zoekt_result = self.index_manager.backend.search_index(
                    prepared_index.index_dir, query, scope, limit
                )
            search_metrics["search_ms"] = (perf_counter() - stage_started) * 1000

            # s16 新增：4. 按查询所用的源码核验行号，再组装带哈希和片段的文件结果。
            search_result["search_exhaustive"] = zoekt_result["exhaustive"]
            search_result["truncated"] = zoekt_result["truncated"]
            search_result["files"] = self._build_file_results(
                zoekt_result["files"], source_snapshot, query, scope, limit
            )
            # s16 新增：显示行被截取也属于省略，不能只在文件数量超限时标记 truncated。
            for file in search_result["files"]:
                if any(hit["snippet_truncated"] for hit in file["hits"]):
                    search_result["truncated"] = True

            # s16 新增：5. 再扫描只为比较版本；若查询期间发生变化，丢弃旧结果而非重试。
            stage_started = perf_counter()
            latest_snapshot = self.workspace.scan()
            search_metrics["verify_ms"] = (perf_counter() - stage_started) * 1000
            if latest_snapshot.fingerprint != source_snapshot.fingerprint:
                # s16 新增：匹配期间改动或删除源码，不把旧位置交给模型继续推理。
                search_result.update(
                    status="stale",
                    files=[],
                    search_exhaustive=False,
                    error="检索期间源码发生变化；请重新检索",
                )
            elif not zoekt_result["exhaustive"]:
                search_result["status"] = "partial"
            else:
                search_result["status"] = "ok" if search_result["files"] else "empty"
        except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            # s16 新增：参数错误与运行故障保留不同状态，不以 (no matches) 掩盖错误。
            status = "invalid" if search_result["status"] == "invalid" else "failed"
            search_result.update(
                status=status, files=[], search_exhaustive=False, error=str(error)[:1000]
            )

        # s16 新增：6. 限制返回正文大小；正文交给模型，指标通过 details 交给会话和事件。
        content = json.dumps(search_result, ensure_ascii=False)
        while len(content.encode()) > MAX_RESULT_BYTES and search_result["files"]:
            search_result["files"].pop()
            search_result["truncated"] = True
            content = json.dumps(search_result, ensure_ascii=False)
        search_metrics.update(
            status=search_result["status"],
            returned_files=len(search_result["files"]),
            truncated=search_result["truncated"],
            search_exhaustive=search_result["search_exhaustive"],
            indexed_files=search_result.get("indexed_files", 0),
            skipped=search_result.get("skipped", {}),
            output_bytes=len(content.encode()),
            duration_ms=(perf_counter() - request_started_at) * 1000,
        )
        if search_result["status"] in {"failed", "invalid", "stale"}:
            content = "Error: " + content
        return ToolResult(content=content, details=search_metrics)

    def _validate(self, query: str, scope: str, limit: int) -> None:
        """只接受字面单行查询和相对目录；不接收 SQL、正则或 shell 参数。"""
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 256
            or any(char in query for char in "\x00\r\n")
        ):
            raise ValueError("query 必须是 1–256 字符的单行字面文本")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1–20 的整数，按文件而不是片段计数")
        if (
            not isinstance(scope, str)
            or len(scope) > 256
            or "\\" in scope
            or ":" in scope
            or scope.startswith("/")
            or ".." in scope.split("/")
            or scope.endswith("/")
            or (scope and (scope == "." or str(PurePosixPath(scope)) != scope))
            or any(char in scope for char in "\x00\r\n")
        ):
            raise ValueError("scope 必须为空或工作区内的相对目录，例如 src/zero2pi")
        if scope and (self.workspace.root / scope).resolve() != self.workspace.root / scope:
            raise ValueError("scope 不能通过链接重定向")

    # s16 新增：只整理 Zoekt 已找到的位置，不在 Python 中另做全仓库搜索或重新排名。
    def _build_file_results(
        self,
        file_matches: list[dict[str, Any]],
        source_snapshot: SourceSnapshot,
        query: str,
        scope: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """将后端的位置列表转换为模型可读的文件结果。

        输入：Zoekt 文件匹配、查询所用的源码快照、查询和范围约束。
        输出：按原排名排列的文件，每项含路径、哈希和最多三段匹配行。
        流程：校验路径 → 核验同版源码中的匹配行 → 截取片段 → 附加哈希。
        边界：不重新搜索或排名；不可信的位置直接报错，截断只影响显示片段。
        """
        file_results = []
        seen_paths = set()
        if len(file_matches) > limit:
            raise RuntimeError("后端超过约定文件上限")
        for file_match in file_matches:
            if not isinstance(file_match, dict):
                raise TypeError("后端匹配必须是对象")
            path = file_match.get("path")
            if (
                not isinstance(path, str)
                or path not in source_snapshot.file_contents
                or path in seen_paths
                or (scope and not path.startswith(scope + "/"))
                or PurePosixPath(path).is_absolute()
            ):
                raise RuntimeError("后端返回未知、重复或越界文件，不信任旧索引证据")
            lines = source_lines(source_snapshot.file_contents[path])
            numbers = file_match.get("lines")
            if not isinstance(numbers, list) or not 1 <= len(numbers) <= 3:
                raise RuntimeError("后端没有返回有效匹配位置")
            snippets = []
            for number in numbers:
                if (
                    type(number) is not int
                    or not 1 <= number <= len(lines)
                    or query not in lines[number - 1]
                ):
                    raise RuntimeError("后端位置与同版源码不一致")
                line = lines[number - 1]
                # s16 新增：围绕命中位置截取，不能只保留行头而把实际证据裁掉。
                position = line.index(query)
                start = max(0, position - 100)
                snippet = line[start : start + MAX_SNIPPET_CHARS]
                snippets.append(
                    {
                        "start_line": number,
                        "end_line": number,
                        "snippet": snippet,
                        "snippet_truncated": start > 0 or start + len(snippet) < len(line),
                        "reason": "字面文本匹配",
                    }
                )
            seen_paths.add(path)
            file_results.append(
                {
                    "path": path,
                    "rank": len(file_results) + 1,
                    "content_hash": source_snapshot.file_hashes[path],
                    "hits": snippets,
                }
            )
        return file_results
