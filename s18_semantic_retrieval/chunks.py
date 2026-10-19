"""s18：把同版源码切成有位置、可复用 embedding 的小块。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from heapq import heappop, heappush

from s16_repository_search.workspace import SourceSnapshot, source_lines
from s17_repository_symbols.entities import CodeEntity, EntityCatalog

MAX_CHUNK_CHARS = 4000
MAX_CHUNKS = 20000


# s18 新增：块只在一次检索中持有正文，持久缓存不保存第二份正文。
@dataclass(frozen=True)
class CodeChunk:
    """描述一段可向量化的源码及其位置。

    输入：路径、起止行、同代实体和源码正文；输出：embedding 输入与内容键。
    流程：build_chunks 创建 → SemanticIndex 编码 → 检索器用原位置返回证据。
    边界：不是新实体；长函数有多个块，但仍关联同一个 CodeEntity。
    """

    path: str
    start_line: int
    end_line: int
    text: str
    entity: CodeEntity | None

    @property
    def embedding_input(self) -> str:
        """路径和符号名参与语义；行号、文件哈希不参与，避免无关改动使缓存全失效。"""
        name = self.entity.qualified_name if self.entity else "module"
        return f"{self.path}\n{name}\n{self.text}"

    @property
    def content_key(self) -> str:
        """缓存键对应实际发送的全部输入，而不是只看文件名。"""
        return hashlib.sha256(self.embedding_input.encode()).hexdigest()


# s18 新增：AST 决定所属实体，窗口只限制大小，不执行目标项目。
def build_chunks(
    snapshot: SourceSnapshot, catalog: EntityCatalog, *, scope: str = ""
) -> list[CodeChunk]:
    """按最内层实体分组，再按行窗口切块，覆盖允许清单中的文本。

    输入：同一次扫描的快照和实体目录；输出：有界、顺序稳定的 CodeChunk 列表。
    流程：逐文件 → 每行关联最内层实体 → 所属实体或字符预算改变时结束一块。
    边界：模块代码和非 Python 文件也保留；单行超过预算时明确拒绝，不悄悄丢源码。
    """
    if catalog.fingerprint != snapshot.fingerprint:
        raise ValueError("切块目录与源码不是同一版本")
    chunks: list[CodeChunk] = []
    for path, data in snapshot.file_contents.items():
        if scope and not path.startswith(scope + "/"):
            continue
        lines = source_lines(data)
        # s18 修改：按起点扫描活动实体，避免深层嵌套时为每个父实体重复填充全部行。
        owners: dict[int, CodeEntity] = {}
        entities = iter(sorted(catalog.by_path.get(path, []), key=lambda item: item.start_line))
        next_entity = next(entities, None)
        active = []
        for number in range(1, len(lines) + 1):
            while next_entity is not None and next_entity.start_line <= number:
                heappush(
                    active,
                    (
                        next_entity.end_line - next_entity.start_line,
                        next_entity.entity_id,
                        next_entity,
                    ),
                )
                next_entity = next(entities, None)
            while active and active[0][2].end_line < number:
                heappop(active)
            if active:
                owners[number] = active[0][2]
        start = 1
        current: list[str] = []
        owner = owners.get(1)
        size = 0
        for number, line in enumerate(lines, 1):
            if len(line) + 1 > MAX_CHUNK_CHARS:
                raise ValueError(f"{path}:{number} 单行超过语义切块预算；可使用 text 模式")
            next_owner = owners.get(number)
            if current and (next_owner != owner or size + len(line) + 1 > MAX_CHUNK_CHARS):
                chunks.append(CodeChunk(path, start, number - 1, "\n".join(current), owner))
                current, start, size = [], number, 0
            owner = next_owner
            current.append(line)
            size += len(line) + 1
        if current and any(line.strip() for line in current):
            chunks.append(CodeChunk(path, start, len(lines), "\n".join(current), owner))
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("语义块超过 20000 个；请缩小目标项目")
    return chunks
