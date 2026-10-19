"""s17：将 Zoekt 分片与 Python 实体按同一源码版本整体发布。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from s16_repository_search.workspace import SourceSnapshot, checked_cache_path
from s17_repository_symbols.entities import EntityCatalog
from s17_repository_symbols.workspace import RepositoryWorkspace
from s17_repository_symbols.zoekt import ZoektBackend


# ===== s17 修改：分片与实体元数据整体发布 =====
# 来自 s16：保持；索引信息沿用磁盘路径和状态；s17 另提供同代实体目录，不将其当作源码副本。
@dataclass(frozen=True)
class PreparedIndex:
    """描述一个已准备好、可供查询的磁盘索引，不保存源码正文。

    输入：索引分片目录、代次、重建状态与 entity_catalog；输出：供检索器查询和关联实体。
    流程：RepositoryIndex.prepare 复用或构建成功后创建 → 查询使用 index_dir。
    边界：保存路径、状态与实体元数据，不是源码副本、Zoekt 索引本体或 Session Entry。
    """

    index_dir: Path
    generation: str
    rebuilt: bool
    # s17 新增：提供同代实体目录；只有边界元数据，不包含源码正文。
    entity_catalog: EntityCatalog


# 来自 s16：保持；以小仓库整代重建换取明确更新边界，不提前实现 Zoekt delta shard 协调。
class RepositoryIndex:
    """复用相同快照的分片，源码变化时构建并激活一个完整新代次。

    输入：workspace、backend 和本次 SourceSnapshot；输出：PreparedIndex。
    流程：计算缓存身份 → 核对已有 manifest/实体 → 临时构建分片和实体 → 成功后原子改名。
    边界：不排名、不读取会话；更新失败不沿用旧分片冒充当前结果。
    """

    def __init__(self, *, workspace: RepositoryWorkspace, backend: ZoektBackend) -> None:
        """保存扫描范围和后端，等待 prepare 显式准备缓存。

        输入：同一目标的 workspace 与已配置 backend；输出：索引管理器。
        流程：仅保存依赖；不在构造时扫描、建索引或创建目录。
        """
        self.workspace = workspace
        self.backend = backend

    def prepare(self, source_snapshot: SourceSnapshot) -> PreparedIndex:
        """取得与本次源码完全对应的分片目录，失败不发布半成品。

        输入：扫描后的字节和内容哈希；输出：可查询的索引代次。
        流程：缓存校验 → 写过滤副本 → 构建分片 → 解析并保存实体 → 保存 manifest → 整体发布。
        来自 s16：保持整代重建；未变化查询与重启可复用，不宣称逐文件增量。
        """
        if self.backend.cache_key is None:
            raise RuntimeError("Zoekt 后端尚未完成 check_ready")
        manifest = {
            # s17 修改：AST 解释器版本也参与代次，不把不同语法版本的解析混用。
            "schema": "s17-v1",
            "parser": f"python-ast/{sys.version_info.major}.{sys.version_info.minor}",
            "workspace": self.workspace.workspace_id,
            "source": source_snapshot.fingerprint,
            "hashes": source_snapshot.file_hashes,
            "backend": self.backend.cache_key,
        }
        generation = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        cache_root = checked_cache_path(self.workspace.index_root)
        destination = checked_cache_path(cache_root / generation)
        manifest_path = checked_cache_path(destination / "manifest.json")
        index_dir = checked_cache_path(destination / "shards")
        # s17 新增：SQLite 与分片位于同一代目录，不能在分片发布后补写元数据。
        entities_path = checked_cache_path(destination / "entities.sqlite")
        # 来自 s16：保持；路径一，代次已存在则校验后直接复用，不重写源码副本或索引。
        if destination.exists():
            if (
                not manifest_path.is_file()
                or manifest_path.stat().st_nlink > 1
                or manifest_path.stat().st_size > 4 * 1024 * 1024
                or json.loads(manifest_path.read_text(encoding="utf-8")) != manifest
                or not index_dir.is_dir()
            ):
                raise RuntimeError("索引缓存无效；请移走该项目缓存后重试，不自动覆盖未知文件")
            _check_shard_files(index_dir)
            if source_snapshot.file_contents and not any(index_dir.glob("*.zoekt")):
                raise RuntimeError("索引分片缺失；未将空缓存当作有效索引")
            # s17 新增：复用前核对数据库版本；缺失或损坏不能悄悄退回无边界结果。
            entity_catalog = EntityCatalog.load(
                entities_path, self.workspace.workspace_id, source_snapshot
            )
            return PreparedIndex(
                index_dir=index_dir,
                generation=generation,
                rebuilt=False,
                entity_catalog=entity_catalog,
            )

        # 来自 s16：保持；路径二，新代次先在临时目录中写过滤副本，再交给 Zoekt 建索引。
        cache_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".build-", dir=cache_root))
        try:
            source_copy_dir = staging / "source"
            source_copy_dir.mkdir()
            staging_index_dir = staging / "shards"
            staging_index_dir.mkdir()
            for relative, data in source_snapshot.file_contents.items():
                path = source_copy_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            if source_snapshot.file_contents:
                self.backend.build_index(source_copy_dir, staging_index_dir)
                _check_shard_files(staging_index_dir)
                if not any(staging_index_dir.glob("*.zoekt")):
                    raise RuntimeError("Zoekt 未生成分片，索引不发布")
            # s17 新增：从相同快照解析并保存实体；任何一步失败都不发布这一代。
            entity_catalog = EntityCatalog.parse(self.workspace.workspace_id, source_snapshot)
            entity_catalog.save(staging / "entities.sqlite")
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            # 来自 s16：保持；仅在完整构建成功后发布，删除文件也不会遗留到新代次。
            staging.rename(destination)
        finally:
            if staging.exists():
                # 来自 s16：保持；只清理本方法刚创建且未发布的临时目录，业务源码不在删除范围。
                if staging.parent != cache_root or staging.resolve() != staging:
                    raise RuntimeError("临时索引目录已重定向，停止清理")
                shutil.rmtree(staging)
        return PreparedIndex(
            index_dir=index_dir, generation=generation, rebuilt=True, entity_catalog=entity_catalog
        )


def _check_shard_files(directory: Path) -> None:
    """缓存也拒绝文件别名；不能把宿主其他文件的硬链接挂进查询容器。"""
    for path in directory.iterdir():
        checked_cache_path(path)
        if not path.is_file() or path.stat().st_nlink > 1:
            raise RuntimeError("索引缓存包含非普通文件或硬链接，未继续检索")
