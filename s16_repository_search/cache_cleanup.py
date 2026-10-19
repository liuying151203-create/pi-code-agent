"""s16 新增：只由用户显式启动的索引清理，不作为模型工具注册。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from s16_repository_search.cache import _iter_cache_files
from s16_repository_search.workspace import RepositoryWorkspace, checked_cache_path

MAX_GENERATIONS = 200
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class CachedGeneration:
    """描述一个已核对身份和目录内容的缓存代次，不保存源码或删除权限。

    输入：代次名称、源码指纹、逻辑大小、目录修改时间；输出：清理排序和预览数据。
    流程：检查 manifest 和文件布局后创建 → 选择保留项 → 确认前后对比。
    """

    name: str
    source_fingerprint: str
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True)
class CacheCleanupPlan:
    """保存本次预览的目标、源码版本及保留/删除清单，不执行删除。

    输入：当前工作区身份、保留数量和已核对的缓存；输出：交给终端预览及执行前复核。
    流程：plan_cache_cleanup 创建 → 用户确认 → apply_cache_cleanup 重算并核对。
    边界：未知、损坏、链接目录只列为 skipped，不加入待删除项。
    """

    index_root: Path
    workspace_id: str
    source_fingerprint: str
    keep_recent: int
    retained: tuple[CachedGeneration, ...]
    remove: tuple[CachedGeneration, ...]
    skipped: tuple[str, ...]


# s16 新增：先计算完整预览，不边枚举边删除；不需要 Docker、模型或会话。
def plan_cache_cleanup(workspace: RepositoryWorkspace, keep_recent: int = 1) -> CacheCleanupPlan:
    """核对本项目、本章缓存，保留当前源码对应代次和最近 N 个有效代次。

    输入：已绑定的工作区、至少为 1 的保留数量；输出：只读清理计划。
    流程：检查限定目录 → 复核当前源码 → 识别有效代次 → 排序并分出保留和删除项。
    边界：当前源码的所有后端版本均保留；未知目录和临时构建目录不清理。
    """
    if type(keep_recent) is not int or keep_recent < 1:
        raise ValueError("至少保留 1 个最近代次")
    root = checked_cache_path(workspace.index_root)
    if root.parent != workspace.root / ".indexes" or root.name not in {"s16", "s17"}:
        raise ValueError("清理范围必须是目标项目内的 s16/s17 索引目录")
    current_fingerprint = workspace.compute_fingerprint()
    generations = []
    skipped = []
    if root.exists():
        if not root.is_dir() or root.is_junction():
            raise ValueError("索引根路径不是独立目录")
        for count, path in enumerate(root.iterdir(), 1):
            if count > MAX_GENERATIONS:
                raise ValueError("索引目录项超过 200 个；本次不执行清理")
            try:
                generations.append(_inspect_generation(path, workspace))
            except (ValueError, TypeError, OSError) as error:
                skipped.append(f"{path.name}: {error}")
    generations.sort(key=lambda item: (item.modified_ns, item.name), reverse=True)
    recent_names = {item.name for item in generations[:keep_recent]}
    retained = []
    remove = []
    for generation in generations:
        if generation.name in recent_names or generation.source_fingerprint == current_fingerprint:
            retained.append(generation)
        else:
            remove.append(generation)
    return CacheCleanupPlan(
        index_root=root,
        workspace_id=workspace.workspace_id,
        source_fingerprint=current_fingerprint,
        keep_recent=keep_recent,
        retained=tuple(retained),
        remove=tuple(remove),
        skipped=tuple(sorted(skipped)),
    )


def apply_cache_cleanup(workspace: RepositoryWorkspace, plan: CacheCleanupPlan) -> int:
    """执行已确认的计划；源码或缓存变化时要求重新预览。

    输入：原工作区和用户确认的计划；输出：完整删除的代次数量。
    流程：重新计算并对比计划 → 逐项检查绝对路径与代次内容 → 删除限定缓存目录。
    边界：不并发运行索引/清理；复核不是文件系统锁。失败可能已删除部分缓存，明确报告。
    """
    if plan.index_root != workspace.index_root or plan.workspace_id != workspace.workspace_id:
        raise ValueError("清理计划不属于当前工作区")
    if plan_cache_cleanup(workspace, plan.keep_recent) != plan:
        raise RuntimeError("源码或缓存已变化；请重新预览，未按旧计划执行清理")
    removed = 0
    try:
        for generation in plan.remove:
            # s16 新增：递归删除前再次验证解析后的绝对路径，只允许本章根目录的直接子项。
            root = checked_cache_path(workspace.index_root)
            path = checked_cache_path(root / generation.name)
            if path.parent != root or _inspect_generation(path, workspace) != generation:
                raise RuntimeError("待清理代次已变化，停止删除")
            shutil.rmtree(path)
            removed += 1
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise RuntimeError(
            f"清理中断，已完整删除 {removed} 个代次，其他缓存可能部分删除；业务源码不在删除范围。"
        ) from error
    return removed


def run_cache_cleanup(workspace: RepositoryWorkspace, keep_recent: int = 1) -> None:
    """展示可回收容量并等待确认，不把清理变成自动行为。

    输入：命令行绑定的工作区与保留数量；输出：终端预览、取消或清理结果。
    流程：生成计划 → 展示目录和候选 → y 确认 → 执行前复核 → 报告。
    边界：不创建索引、不启动后端；空输入、EOF、中断和其他回答均不执行删除。
    """
    plan = plan_cache_cleanup(workspace, keep_recent)
    print(f"索引目录：{plan.index_root}")
    print(
        f"保留当前源码对应的全部代次及最近 {keep_recent} 个有效代次，共 {len(plan.retained)} 个。"
    )
    if plan.skipped:
        print(f"另有 {len(plan.skipped)} 项无法识别或不安全，保持不动：")
        for item in plan.skipped:
            print(f"  {item}")
    if not plan.remove:
        print("没有可清理的旧索引。")
        return
    total_bytes = sum(item.size_bytes for item in plan.remove)
    print(f"拟删除 {len(plan.remove)} 个旧代次，共 {total_bytes} 字节：")
    for item in plan.remove:
        print(f"  {item.name}  {item.size_bytes} 字节")
    print("包含旧的过滤源码副本；删除不进回收站，不能据此恢复已丢失的历史源码。")
    try:
        answer = input("是否清理这些缓存？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "y":
        print("已取消，未删除缓存。")
        return
    removed = apply_cache_cleanup(workspace, plan)
    print(f"已删除 {removed} 个旧索引代次；业务源码未改动，可按当前源码重新建立索引。")


def _inspect_generation(path: Path, workspace: RepositoryWorkspace) -> CachedGeneration:
    """只识别本项目、本章完整发布的代次；额外文件、链接或异常 manifest 都保留。"""
    if path.parent != workspace.index_root or not _is_hash(path.name):
        raise ValueError("不是已发布的索引代次")
    files = dict(_iter_cache_files(path))
    if not 0 < files.get("manifest.json", 0) <= MAX_MANIFEST_BYTES:
        raise ValueError("manifest 缺失或超过预算")
    # s16 新增：实际读取也有上限，不能仅依赖枚举时的旧大小。
    manifest = _read_manifest(path / "manifest.json")
    chapter = workspace.index_root.name
    expected_fields = {"schema", "workspace", "source", "hashes", "backend"}
    if chapter == "s17":
        expected_fields.add("parser")
    if (
        set(manifest) != expected_fields
        or manifest.get("schema") != f"{chapter}-v1"
        or manifest.get("workspace") != workspace.workspace_id
        or not _is_hash(manifest.get("source"))
        or not isinstance(manifest.get("hashes"), dict)
        or len(manifest["hashes"]) > 5000
        or any(not _is_hash(value) for value in manifest["hashes"].values())
    ):
        raise ValueError("manifest 身份或格式不属于本项目、本章")
    if (
        hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != path.name
        or hashlib.sha256(json.dumps(manifest["hashes"], sort_keys=True).encode()).hexdigest()
        != manifest["source"]
    ):
        raise ValueError("manifest 与代次或源码指纹不一致")
    source_files = {f"source/{name}" for name in manifest["hashes"]}
    if {name for name in files if name.startswith("source/")} != source_files:
        raise ValueError("过滤副本缺失或包含未知文件")
    allowed = source_files | {"manifest.json"}
    if chapter == "s17":
        if "entities.sqlite" not in files:
            raise ValueError("实体数据库缺失")
        allowed.add("entities.sqlite")
    for name in files:
        if Path(name).parent.as_posix() == "shards" and name.endswith((".zoekt", ".zoekt.meta")):
            allowed.add(name)
    if set(files) != allowed or not (path / "source").is_dir() or not (path / "shards").is_dir():
        raise ValueError("缓存布局不完整或包含未知文件")
    if source_files and not any(
        name.startswith("shards/") and name.endswith(".zoekt") for name in files
    ):
        raise ValueError("索引分片缺失")
    return CachedGeneration(
        path.name, manifest["source"], sum(files.values()), path.stat().st_mtime_ns
    )


def _read_manifest(path: Path) -> dict:
    """有界读取普通独立文件；枚举后变大或被重定向不能绕过清理预检。"""
    checked_cache_path(path)
    before = path.stat()
    with path.open("rb") as file:
        opened = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("manifest 不是独立普通文件或读取时发生替换")
        checked_cache_path(path)
        data = file.read(MAX_MANIFEST_BYTES + 1)
    checked_cache_path(path)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest 读取时超过预算")
    manifest = json.loads(data.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError("manifest 必须是 JSON 对象")
    return manifest


def _is_hash(value: object) -> bool:
    """只接受规范的小写 SHA-256 名称，不能把任意相对路径作为代次。"""
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )
