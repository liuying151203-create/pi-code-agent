"""s16 新增：Python 调用本地 Zoekt 镜像；不拉取镜像、不启动常驻服务。"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

# ===== s16 新增：固定后端版本与短进程调用 =====
# s16 新增：版本固定且镜像需要预先准备，不让查询自动变成下载/安装工作流。
ZOEKT_COMMIT = "27517a30ae6df16c6e768623847fdab8f6a3bbcf"
DEFAULT_IMAGE = "zero2pi-zoekt:s16"


# s16 新增：相对 Sourcebot 的常驻 gRPC 服务，本章用离线短进程，减少部署和组装层次。
class ZoektBackend:
    """通过受限短容器建立索引和执行字面搜索，算法由 Zoekt 实现。

    输入：用户预先构建的镜像、过滤后的源码目录、独立分片目录、查询。
    输出：构建完成，或含文件/行号与完整性标记的搜索数据。
    流程：检查本地镜像/固定提交 → 容器调用 zoekt-index → 容器调用薄搜索适配器。
    边界：无模型 API、无 shell 拼接、无网络、无目标原仓库挂载；不自动下载和构建。
    """

    def __init__(self, image: str = DEFAULT_IMAGE) -> None:
        """保存镜像配置；实际检查在入口显式调用 check_ready。"""
        self.image = image
        self.cache_key: str | None = None

    def check_ready(self) -> None:
        """确认本地 Docker Linux 镜像，并固定本次使用的镜像 ID。

        输入：镜像名；输出：无，保存 cache_key 或明确报错。
        流程：只做 image inspect → 核对构建标签 → 保存不可变镜像 ID。
        标签同名但镜像不同会使用新的缓存版本，不声称当前安装了最新版 Zoekt。
        """
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.image],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(
                "需要本机 Docker Linux 服务及预先构建的 s16 镜像；见章节 README"
            ) from error
        if result.returncode != 0:
            raise RuntimeError("s16 Zoekt 镜像不可用；请按 README 手动构建，程序不会拉取镜像")
        try:
            metadata = json.loads(result.stdout)[0]
            if not isinstance(metadata, dict) or not isinstance(metadata.get("Config"), dict):
                raise TypeError("镜像元数据结构无效")
            labels = metadata["Config"].get("Labels") or {}
            if not isinstance(labels, dict):
                raise TypeError("镜像标签结构无效")
            image_id = metadata["Id"]
            if (
                metadata["Os"] != "linux"
                or labels.get("zero2pi.zoekt.commit") != ZOEKT_COMMIT
                or labels.get("zero2pi.zoekt.adapter") != "s16-v1"
                or not isinstance(image_id, str)
                or not image_id.startswith("sha256:")
            ):
                raise ValueError("镜像版本不匹配")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError("镜像不是本章固定版本，请用本章 Dockerfile 重新构建") from error
        self.cache_key = image_id

    # s16 新增：方法名说明只调用索引引擎，不承担缓存复用和发布决策。
    def build_index(self, source_copy_dir: Path, index_dir: Path) -> None:
        """用官方目录索引器索引源码副本，包括未提交的合规文件。

        输入：过滤副本和新分片目录；输出：无，成功后由 RepositoryIndex 激活缓存。
        流程：只读挂载副本/可写挂载新分片 → 运行 zoekt-index → 检查退出状态。
        s16 不生成 ctags；符号边界属于 s17，不提前加入未使用的符号数据。
        """
        mounts = [(source_copy_dir, "/source", True), (index_dir, "/index", False)]
        arguments = [
            "-index",
            "/index",
            "-disable_ctags",
            "-parallelism",
            "1",
            "-file_limit",
            "1048576",
            "-max_trigram_count",
            "1048576",
            "/source",
        ]
        self._run("/zoekt-index", arguments, mounts, timeout=120)

    # s16 新增：区别于 CodeRetriever.search，只查询已准备好的磁盘索引。
    def search_index(self, index_dir: Path, query: str, scope: str, limit: int) -> dict[str, Any]:
        """搜索当前代次分片，返回位置和后端完整性，不转换成模型正文。

        输入：索引目录、字面文本、可选目录范围和文件上限；输出：适配器 JSON 对象。
        流程：JSON 通过 stdin 传参 → 只读挂载索引 → 解析并校验结果外形。
        适配器构造 Substring，而非解析模型给出的 Zoekt 表达式或正则。
        """
        request = json.dumps({"query": query, "scope": scope, "limit": limit}, ensure_ascii=False)
        output = self._run(
            "/search", [], [(index_dir, "/index", True)], timeout=30, input_text=request
        )
        try:
            result = json.loads(output)
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("files"), list)
                or type(result.get("exhaustive")) is not bool
                or type(result.get("truncated")) is not bool
            ):
                raise ValueError("搜索返回结构错误")
        except (ValueError, TypeError) as error:
            raise RuntimeError("Zoekt 搜索适配器未返回有效 JSON，不当作空结果") from error
        return result

    def _run(
        self,
        entrypoint: str,
        arguments: list[str],
        mounts: list[tuple[Path, str, bool]],
        *,
        timeout: int,
        input_text: str | None = None,
    ) -> str:
        """运行固定程序；超时/中断尝试清理本次容器，无法确认时明确报告。"""
        if self.cache_key is None:
            raise RuntimeError("请先 check_ready，不允许在运行中自动准备环境")
        name = "zero2pi-s16-" + uuid.uuid4().hex
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=1g",
            "--cpus=2",
            "--pids-limit=64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
        ]
        if hasattr(os, "getuid"):
            # s16 新增：Linux 下由当前用户写缓存，避免生成只能由 root 删除的宿主分片。
            command += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for source, destination, read_only in mounts:
            if not source.is_absolute() or source.resolve() != source or "," in str(source):
                raise ValueError("Docker 挂载路径必须是无逗号、无重定向的绝对路径")
            mount = f"type=bind,source={source},target={destination}"
            command += ["--mount", mount + (",readonly" if read_only else "")]
        command += ["-i", "--entrypoint", entrypoint, self.cache_key, *arguments]
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # s16 新增：只删除此次随机命名的临时容器，不操作用户项目或其他容器。
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError(f"Zoekt 超时或中断，未确认容器 {name} 已清理") from error
            if cleanup.returncode != 0:
                raise RuntimeError(f"Zoekt 超时或中断，未确认容器 {name} 已清理") from None
            raise RuntimeError("Zoekt 进程超时或中断，已停止本次检索") from None
        if result.returncode != 0:
            raise RuntimeError(f"Zoekt 执行失败：{result.stderr[-1000:].strip()}")
        return result.stdout
