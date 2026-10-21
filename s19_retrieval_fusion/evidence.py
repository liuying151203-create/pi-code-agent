"""s19：候选合并、可解释 RRF 和有界证据组装；不执行搜索或扫描。"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from s16_repository_search.evidence import CodeEvidence
from s16_repository_search.workspace import SourceSnapshot, source_lines

MAX_RESULT_BYTES = 16 * 1024


# ===== s19 新增：融合数据与策略 =====
# s19 新增：策略只配置已经实现的行为；远程授权和文件访问边界不由排序配置控制。
@dataclass(frozen=True)
class FusionPolicy:
    """约束通道排名与输出规模，不承担检索或语言解析。

    输入：RRF 常数、各通道权重、每文件片段数和显示长度。
    输出：EvidencePipeline 使用的已验证策略。
    流程：构造时拒绝非法值 → rank 读取权重 → pack 执行预算。
    边界：权重不是置信概率；text/symbol 共用引擎，不宣称两份独立事实。
    """

    rrf_k: int = 60
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"text": 1.0, "symbol": 1.0, "semantic": 1.0}
    )
    hits_per_file: int = 3
    snippet_chars: int = 500

    def __post_init__(self):
        """只接受实际通道和正的有限数值，防止 NaN 或零预算绕过排序。"""
        if (
            type(self.rrf_k) is not int
            or self.rrf_k < 1
            or type(self.hits_per_file) is not int
            or not 1 <= self.hits_per_file <= 10
            or type(self.snippet_chars) is not int
            or not 1 <= self.snippet_chars <= 2000
            or not isinstance(self.weights, Mapping)
            or set(self.weights) != {"text", "symbol", "semantic"}
            or any(
                type(v) not in (float, int) or not math.isfinite(v) or v <= 0
                for v in self.weights.values()
            )
        ):
            raise ValueError("融合策略必须包含合法的正数排名权重与显示预算")
        # s19 新增：复制并冻结权重，避免调用方修改原字典让同一策略悄悄改变。
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))


# s19 新增：原始相似度随候选保存，但绝不与 Zoekt 分数直接相加。
@dataclass(frozen=True)
class Candidate:
    """保存一路召回的一处匹配，不承担实体聚合。

    输入：CodeEvidence 与可选语义相似度；输出：交给 EvidencePipeline.rank 的候选。
    流程：检索器从当前快照创建 → 按通道排列 → 管线读取但不修改。
    边界：原始相似度只解释语义来源，不直接与其他引擎分数相加。
    """

    evidence: CodeEvidence
    similarity: float | None = None


@dataclass
class RankedCandidate:
    """聚合一个代码实体及其全部命中位置。

    输入：同文件版本的 Candidate 和各路名次；输出：供 pack 组织上下文的排名项。
    流程：rank 按实体归组 → 累积不同位置 → 计算实体级 RRF → pack 选择显示片段。
    边界：没有实体时按片段范围归组；排名单位与显示片段单位不同。
    """

    matches: list[Candidate] = field(default_factory=list)
    channel_ranks: dict[str, int] = field(default_factory=dict)
    score: float = 0.0


# ===== s19 新增：实体聚合与证据组装 =====
# s19 新增：参考 Sourcebot 的结果适配和 Potpie 的证据组装职责，采用小型独立管线。
class EvidencePipeline:
    """把各路召回变成可追溯、有预算的代码上下文。

    输入：FusionPolicy，以及同版本 Candidate、源码快照和可选关系证据。
    输出：rank 返回实体候选；pack 返回带省略原因的标准结果。
    流程：按实体聚合但保留片段 → 记录通道名次 → RRF → 组织文件 → 补充关系 → 裁剪。
    边界：不调用引擎、embedding 或模型；关系是结构补充，不伪造成相关度排名通道。
    """

    def __init__(self, policy: FusionPolicy):
        """保存显式策略，不读取文件。"""
        self.policy = policy

    def rank(self, channels: dict[str, list[Candidate]]) -> list[RankedCandidate]:
        """按实体融合通道名次，同时保留同一路和跨路的不同证据位置。

        输入：各通道按相关度排列的候选；输出：排序后的独立聚合对象。
        流程：以文件版本＋实体或片段位置分组 → 每路每实体记一次名次 → 收集片段 → 加权 RRF。
        边界：不修改调用方候选；同一路重复命中不多投票；不同版本的文件禁止合并。
        """
        groups = {}
        versions = {}
        for channel, candidates in channels.items():
            if channel not in self.policy.weights:
                raise ValueError("未知召回通道")
            seen = set()
            for candidate in candidates:
                evidence = candidate.evidence
                if evidence.source != channel:
                    raise ValueError("证据来源与召回通道不一致")
                previous_hash = versions.setdefault(evidence.path, evidence.content_hash)
                if previous_hash != evidence.content_hash:
                    raise ValueError("不能融合不同版本的文件证据")
                entity = evidence.entity
                key = (
                    evidence.path,
                    evidence.content_hash,
                    entity.entity_id if entity else (evidence.start_line, evidence.end_line),
                )
                group = groups.setdefault(key, RankedCandidate())
                if key not in seen:
                    seen.add(key)
                    group.channel_ranks[channel] = len(seen)
                # s19 新增：不能在 seen 命中后直接 continue，否则长函数的后续代码块会丢失。
                if candidate not in group.matches:
                    group.matches.append(candidate)
        for group in groups.values():
            group.score = sum(
                self.policy.weights[channel] / (self.policy.rrf_k + rank)
                for channel, rank in group.channel_ranks.items()
            )
        return sorted(
            groups.values(),
            key=lambda item: (
                -item.score,
                item.matches[0].evidence.path,
                item.matches[0].evidence.start_line,
            ),
        )

    def pack(
        self,
        payload: dict,
        ranked: list[RankedCandidate],
        snapshot: SourceSnapshot,
        *,
        limit: int,
        relations: list[dict] | None = None,
    ) -> dict:
        """按文件组织主要证据，再在剩余预算内附加关系，不改变候选排名。

        输入：结果元数据、已排序候选、同版源码、文件上限、可选关系。
        输出：files、related_context、omissions；总 JSON 不超过 16 KiB。
        流程：核对证据身份 → 合并相同片段的来源 → 文件/片段限额 → 总量裁剪 → 添加关系。
        边界：关系不挤掉主要结果；省略与未找到不同；最终源码复核由调用方负责。
        """
        payload = {
            **payload,
            "files": [],
            "related_context": [],
            "omissions": {"file_limit": 0, "hit_limit": 0, "snippet_chars": 0, "byte_budget": 0},
        }
        by_path = {}
        # s19 修改：同一文件只按 LF 计行一次，避免每个候选重复拆分大文件。
        line_counts = {}
        for group in ranked:
            spans = {}
            for match in group.matches:
                evidence = match.evidence
                if evidence.path in snapshot.file_contents and evidence.path not in line_counts:
                    line_counts[evidence.path] = len(
                        source_lines(snapshot.file_contents[evidence.path])
                    )
                if (
                    snapshot.file_hashes.get(evidence.path) != evidence.content_hash
                    or type(evidence.start_line) is not int
                    or type(evidence.end_line) is not int
                    or not 1
                    <= evidence.start_line
                    <= evidence.end_line
                    <= line_counts.get(evidence.path, 0)
                    or (
                        evidence.entity is not None
                        and (
                            evidence.entity.path != evidence.path
                            or evidence.entity.content_hash != evidence.content_hash
                        )
                    )
                ):
                    raise ValueError("证据不属于本次源码版本或范围")
                span = (evidence.start_line, evidence.end_line, evidence.snippet)
                if span not in spans:
                    hit = evidence.to_hit()
                    hit.update(
                        sources=[],
                        channel_ranks=dict(group.channel_ranks),
                        rrf_score=group.score,
                        similarities={},
                    )
                    spans[span] = hit
                hit = spans[span]
                if evidence.source not in hit["sources"]:
                    hit["sources"].append(evidence.source)
                if match.similarity is not None:
                    hit["similarities"][evidence.source] = match.similarity
            for hit in spans.values():
                path = group.matches[0].evidence.path
                if path not in by_path and len(by_path) >= limit:
                    payload["omissions"]["file_limit"] += 1
                    continue
                file = by_path.setdefault(
                    path,
                    {
                        "path": path,
                        "rank": len(by_path) + 1,
                        "content_hash": snapshot.file_hashes[path],
                        "hits": [],
                    },
                )
                if len(file["hits"]) >= self.policy.hits_per_file:
                    payload["omissions"]["hit_limit"] += 1
                    continue
                if len(hit["snippet"]) > self.policy.snippet_chars:
                    hit["snippet"] = hit["snippet"][: self.policy.snippet_chars]
                    hit["snippet_truncated"] = True
                if hit["snippet_truncated"]:
                    payload["omissions"]["snippet_chars"] += 1
                file["hits"].append(hit)
        payload["files"] = list(by_path.values())
        payload["truncated"] |= any(payload["omissions"].values())
        # s19 新增：优先保留主要召回；关系只能使用剩余空间，不把文件排名与关系顺序混算。
        while self._size(payload) > MAX_RESULT_BYTES and payload["files"]:
            payload["files"].pop()
            payload["truncated"] = True
            payload["omissions"]["byte_budget"] += 1
        for relation in relations or []:
            payload["related_context"].append(relation)
            if self._size(payload) > MAX_RESULT_BYTES:
                payload["related_context"].pop()
                payload["truncated"] = True
                payload["omissions"]["byte_budget"] += 1
        if self._size(payload) > MAX_RESULT_BYTES:
            raise ValueError("结果元数据超过 16 KiB 预算")
        return payload

    @staticmethod
    def _size(payload):
        """按真实 UTF-8 JSON 字节计预算，不用字符数冒充 Token。"""
        return len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())


# s19 新增：s22 的文件级对照复用此排名入口，评测器不再自行定义另一套融合算法。
def fuse_files(channels: list[list[str]]) -> list[str]:
    """融合文件名列表，供固定组件评测使用，不代替实体级证据组装。

    输入：每路有序文件名；输出：按 RRF 排列的去重文件名。
    流程：路内去重 → 名次贡献相加 → 文件名稳定打破同分。
    边界：不把关系边变成独立相关性评分；调用方明确选择评测的比较策略。
    """
    scores = {}
    for files in channels:
        for rank, path in enumerate(dict.fromkeys(files), 1):
            scores[path] = scores.get(path, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda path: (-scores[path], path))
