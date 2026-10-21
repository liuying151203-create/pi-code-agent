"""s16：文本、符号和后续召回共用的最小代码证据，不包含排名策略。"""

from dataclasses import asdict, dataclass

from s16_repository_search.entities import CodeEntity


# s16 新增：先统一位置、文件版本和来源；召回名次及跨路融合在 s19 引入。
@dataclass(frozen=True)
class CodeEvidence:
    """保存一处可补读的源码命中，不把函数身份当成唯一的命中位置。

    输入：文件路径、行范围、显示片段、文件哈希、来源和可选实体。
    输出：供各章返回或交给 s19 合并的不可变证据。
    流程：同版快照校验位置 → 创建证据 → to_hit 输出公共字段。
    边界：片段可能截短；实体范围不等于命中范围；不比较不同引擎原始分数。
    """

    path: str
    start_line: int
    end_line: int
    snippet: str
    content_hash: str
    source: str
    entity: CodeEntity | None = None
    snippet_truncated: bool = False

    def to_hit(self) -> dict:
        """转为模型结果字段；文件身份也保留在上层 files 条目中。"""
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "snippet_truncated": self.snippet_truncated,
            "source": self.source,
            "reason": {
                "text": "字面文本匹配",
                "symbol": "符号定义匹配",
                "semantic": "语义相似匹配",
            }[self.source],
            "entity": asdict(self.entity) if self.entity is not None else None,
        }
