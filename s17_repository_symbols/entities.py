"""s17 新增：解析 Python 定义，把同版代码边界保存为可重建的 SQLite 元数据。"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from s16_repository_search.workspace import SourceSnapshot, checked_cache_path, source_lines

# ===== s17 新增：实体与解析目录 =====
# s17 新增：限制实际保存的元数据数量；超限明确失败，不发布不完整目录。
MAX_ENTITIES = 50000
MAX_CATALOG_BYTES = 64 * 1024 * 1024
CATALOG_SCHEMA = "s17-entities-v1"


# s17 新增：同名定义靠路径、限定名、定义行和文件版本区分，不把名称当主键。
@dataclass(frozen=True)
class CodeEntity:
    """描述一个 Python 类或函数的身份与完整源码范围，不缓存源码正文。

    输入：路径、名称、限定名、种类、行号、文件哈希和实体 ID；输出：不可变元数据。
    流程：EntityCatalog.parse 创建 → SQLite 保存/加载 → 检索命中按行关联 → 模型补读。
    边界：start_line 包含装饰器，definition_line 是 def/class 行，end_line 包含结束行；
    ID 属于当前文件版本，不承诺跨重命名或源码修改保持不变。
    """

    entity_id: str
    path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    definition_line: int
    end_line: int
    content_hash: str


# s17 新增：参考 Sourcebot 的符号定位，额外用标准库 AST 提供可靠的 Python 完整边界。
class EntityCatalog:
    """管理一个源码版本中的 Python 实体，以及无法解析的文件说明。

    输入：项目身份、源码指纹、实体和解析错误；输出：可保存、恢复、按命中行查询的目录。
    流程：parse 仅解析合规快照 → save 随索引发布 → load 复用 → entity_at_line 关联命中。
    边界：不 import/执行目标代码，不负责关键词搜索，不生成调用图；正文仍来自源码快照。
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        fingerprint: str,
        entities: list[CodeEntity],
        parse_errors: dict[str, str],
    ) -> None:
        """保存同代元数据并建立内存中的按文件分组。

        输入：项目身份、源码指纹、实体列表和解析错误；输出：待查询的实体目录。
        流程：保存元信息 → 按 path 建 by_path；命中关联不再遍历全仓库实体。
        边界：不解析、不写数据库，也不保存源码正文。
        """
        self.workspace_id = workspace_id
        self.fingerprint = fingerprint
        self.entities = entities
        self.parse_errors = parse_errors
        self.by_path: dict[str, list[CodeEntity]] = {}
        for entity in entities:
            self.by_path.setdefault(entity.path, []).append(entity)

    @classmethod
    def parse(cls, workspace_id: str, snapshot: SourceSnapshot) -> EntityCatalog:
        """从本次快照提取类、普通/异步函数，保留嵌套定义的限定名。

        输入：目标身份和合规源码字节；输出：实体目录；语法错误记在 parse_errors 中。
        流程：逐个 .py/.pyi 文件解析 → 显式栈遍历 AST → 记录范围与身份 → 按文件组装。
        边界：单文件解析失败不妨碍它参与文本搜索；实体总量超限则整代构建失败。
        """
        entities: list[CodeEntity] = []
        parse_errors: dict[str, str] = {}
        for path, data in snapshot.file_contents.items():
            if Path(path).suffix not in {".py", ".pyi"}:
                continue
            try:
                # s17 新增：只处理 UTF-8 源码，允许 BOM；AST 不执行项目中的任何语句。
                tree = ast.parse(data.decode("utf-8-sig"), filename=path)
            except (SyntaxError, ValueError, RecursionError) as error:
                line = getattr(error, "lineno", None)
                message = getattr(error, "msg", type(error).__name__)
                parse_errors[path] = f"第 {line or '?'} 行：{message}"[:200]
                continue
            # s17 新增：栈中的 names 是外层类/函数名，不靠辅助函数递归隐藏限定名的形成。
            pending: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
            while pending:
                node, names = pending.pop()
                child_names = names
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_names = (*names, node.name)
                    qualified_name = ".".join(child_names)
                    start_line = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    if isinstance(node, ast.AsyncFunctionDef):
                        kind = "async_function"
                    content_hash = snapshot.file_hashes[path]
                    identity = [workspace_id, path, qualified_name, node.lineno, content_hash]
                    entity_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                    entities.append(
                        CodeEntity(
                            entity_id=entity_id,
                            path=path,
                            name=node.name,
                            qualified_name=qualified_name,
                            kind=kind,
                            start_line=start_line,
                            definition_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            content_hash=content_hash,
                        )
                    )
                    if len(entities) > MAX_ENTITIES:
                        raise RuntimeError("Python 实体超过 50000 个；请缩小目标范围")
                pending.extend(
                    (child, child_names) for child in reversed(list(ast.iter_child_nodes(node)))
                )
        return cls(
            workspace_id=workspace_id,
            fingerprint=snapshot.fingerprint,
            entities=entities,
            parse_errors=parse_errors,
        )

    def entity_at_line(
        self, path: str, line: int, *, symbol_query: str | None = None
    ) -> CodeEntity | None:
        """将引擎命中关联到最内层实体，不执行另一轮关键词检索。

        输入：已有匹配的路径、行号，符号模式另带字面名称片段；输出：实体或 None。
        流程：只看该文件元数据 → 文本命中取包含它的最小范围；符号命中只关联定义行。
        边界：模块级文本、变量符号、非 Python 或解析失败文件可没有实体，不能编造边界。
        """
        candidates = self.by_path.get(path, [])
        if symbol_query is not None:
            candidates = [
                entity
                for entity in candidates
                if entity.definition_line == line and symbol_query in entity.name
            ]
        else:
            candidates = [e for e in candidates if e.start_line <= line <= e.end_line]
        return min(
            candidates, key=lambda e: (e.end_line - e.start_line, -e.start_line), default=None
        )

    # ===== s17 新增：与 Zoekt 分片一同发布的实体数据库 =====
    def save(self, path: Path) -> None:
        """把目录写入新建代次的 SQLite，不保存代码正文或预留后续图表。

        输入：RepositoryIndex 创建的临时目录中的新文件路径；输出：已提交并关闭的数据库。
        流程：拒绝覆盖 → 创建元信息/实体/解析错误三张表 → 写入 → 提交；发布由管理器负责。
        """
        checked_cache_path(path)
        if path.exists():
            raise RuntimeError("实体数据库已存在，不覆盖未知缓存")
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, path TEXT, name TEXT, "
                "qualified_name TEXT, kind TEXT, start_line INTEGER, definition_line INTEGER, "
                "end_line INTEGER, content_hash TEXT);"
                "CREATE TABLE parse_errors (path TEXT PRIMARY KEY, error TEXT);"
            )
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    ("schema", CATALOG_SCHEMA),
                    ("workspace", self.workspace_id),
                    ("source", self.fingerprint),
                ],
            )
            connection.executemany(
                "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(asdict(entity).values()) for entity in self.entities],
            )
            connection.executemany(
                "INSERT INTO parse_errors VALUES (?, ?)", self.parse_errors.items()
            )
            connection.commit()
        finally:
            connection.close()
        # s17 新增：发布前也检查容量，不能生成下一次查询无法加载的数据库。
        if path.stat().st_size > MAX_CATALOG_BYTES:
            raise RuntimeError("实体数据库超过 64 MiB；请缩小目标范围")

    @classmethod
    def load(cls, path: Path, workspace_id: str, snapshot: SourceSnapshot) -> EntityCatalog:
        """只读恢复当前代的实体，不重新解析全部源码，也不接受另一版本元数据。

        输入：已发布数据库、目标身份、当前快照；输出：校验后的 EntityCatalog。
        流程：核对路径/普通文件/大小 → 只读打开 → 核对元信息 → 有界读取 → 核对路径和范围。
        边界：损坏或缺失时报错，不用旧实体解释新索引；不自动修复未知缓存。
        """
        checked_cache_path(path)
        if (
            not path.is_file()
            or path.stat().st_nlink > 1
            or path.stat().st_size > MAX_CATALOG_BYTES
        ):
            raise RuntimeError("实体数据库缺失、过大或不是独立普通文件")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            expected = {
                "schema": CATALOG_SCHEMA,
                "workspace": workspace_id,
                "source": snapshot.fingerprint,
            }
            if dict(connection.execute("SELECT key, value FROM metadata")) != expected:
                raise RuntimeError("实体数据库与当前源码版本不一致")
            rows = connection.execute(
                "SELECT * FROM entities LIMIT ?", (MAX_ENTITIES + 1,)
            ).fetchall()
            if len(rows) > MAX_ENTITIES:
                raise RuntimeError("实体数据库超过数量上限")
            entities = [CodeEntity(*row) for row in rows]
            errors = dict(connection.execute("SELECT path, error FROM parse_errors LIMIT 5001"))
        except sqlite3.Error as error:
            raise RuntimeError("实体数据库损坏，未继续检索") from error
        finally:
            connection.close()
        # s17 新增：同一文件只统计一次行数，避免有大量定义时反复切分完整源码。
        line_counts = {
            path: len(source_lines(snapshot.file_contents[path]))
            for path in {entity.path for entity in entities}
            if path in snapshot.file_contents
        }
        for entity in entities:
            if (
                entity.path not in snapshot.file_contents
                or entity.content_hash != snapshot.file_hashes[entity.path]
                or not all(
                    type(n) is int
                    for n in (entity.start_line, entity.definition_line, entity.end_line)
                )
                or not 1
                <= entity.start_line
                <= entity.definition_line
                <= entity.end_line
                <= line_counts[entity.path]
                or entity.kind not in {"class", "function", "async_function"}
                or not isinstance(entity.name, str)
                or not entity.name.isidentifier()
                or not isinstance(entity.qualified_name, str)
                or entity.qualified_name.split(".")[-1] != entity.name
            ):
                raise RuntimeError("实体内容与当前源码不一致")
            # s17 新增：缓存中的实体 ID 也必须对应当前文件版本，不能仅检查范围。
            identity = [
                workspace_id,
                entity.path,
                entity.qualified_name,
                entity.definition_line,
                entity.content_hash,
            ]
            if entity.entity_id != hashlib.sha256(json.dumps(identity).encode()).hexdigest():
                raise RuntimeError("实体身份与当前源码不一致")
        if any(
            path not in snapshot.file_contents or not isinstance(error, str)
            for path, error in errors.items()
        ):
            raise RuntimeError("解析错误记录包含未知文件或无效说明")
        return cls(
            workspace_id=workspace_id,
            fingerprint=snapshot.fingerprint,
            entities=entities,
            parse_errors=errors,
        )
