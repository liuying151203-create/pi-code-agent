"""s13-A 新增：固定 Docker 执行边界，不把模型命令交给宿主 shell。"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from pi_code_agent.sandbox_copy.snapshot import WorkspaceSnapshot

DEFAULT_IMAGE = "pi-code-agent-sandbox:copy"
WORKER = "/opt/pi-code-agent/src/pi_code_agent/sandbox_copy/tool_worker.py"
# s13-A 新增：覆盖有界 base64 快照；工具调用本身仍使用 s02 的 50 KiB 返回预算。
MAX_DOCKER_OUTPUT = 20 * 1024 * 1024
TOOL_NAMES = {"bash", "read_file", "grep", "glob", "write_file", "edit_file"}


class SandboxError(RuntimeError):
    """隔离执行失败；调用方必须停止，不能换到宿主执行。"""


def _collect_output(process: subprocess.Popen, output: bytearray, exceeded: Event) -> None:
    """持续读取 CLI 输出，超出宿主内存预算即终止 CLI。"""
    while chunk := process.stdout.read(65536):
        available = MAX_DOCKER_OUTPUT - len(output)
        output.extend(chunk[:available])
        if len(chunk) > available:
            exceeded.set()
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return


def _send_input(process: subprocess.Popen, data: bytes) -> None:
    """在独立线程写入 stdin，避免未读取输入的进程绕过主线程超时。"""
    try:
        process.stdin.write(data)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass


def run_docker(arguments: list[str], *, data: bytes | None = None, timeout: int = 30) -> bytes:
    """执行由 Harness 构造的 Docker argv，不使用宿主 shell。

    输入：固定 Docker 子命令、可选 stdin 数据、超时；输出：有界 stdout/stderr bytes。
    流程：启动 CLI → 并行读写管道 → 限时等待 → 检查退出码和输出上限。
    边界：杀掉 CLI 不等于杀掉容器；SandboxExecutor 的 finally 负责移除指定容器。
    """
    try:
        process = subprocess.Popen(
            arguments,
            shell=False,
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        raise SandboxError("无法启动 Docker CLI；请先手动安装并启动 Docker Desktop") from error

    output = bytearray()
    exceeded = Event()
    reader = Thread(target=_collect_output, args=(process, output, exceeded), daemon=True)
    reader.start()
    if data is not None:
        Thread(target=_send_input, args=(process, data), daemon=True).start()
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        process.kill()
        process.wait()
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SandboxError(f"Docker 操作超过 {timeout} 秒；本次任务必须停止") from error
    finally:
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()
    if exceeded.is_set() or reader.is_alive():
        raise SandboxError("Docker 输出超限或管道未关闭；本次任务必须停止")
    if process.returncode:
        diagnostic = bytes(output[:2000]).decode("utf-8", errors="replace")
        raise SandboxError(f"Docker 执行失败：{diagnostic}")
    return bytes(output)


@dataclass
class SandboxExecutor:
    """在单个受限容器中执行全部模型工具，并只导出审查用 Diff。

    输入：用户准备的本地镜像，以及可替换的 Docker CLI 函数（离线测试使用）。
    输出：工具文本结果和 changes.diff；不向原仓库应用任何修改。
    流程：start 复制快照 → dispatch 执行工具 → capture_workspace 导出内容 → close 清理。
    回写由 WorkspaceSync 在宿主确认后负责，不属于执行器职责。
    边界：模型不能设置镜像、挂载、网络或 Docker 参数；无宿主执行回退。
    """

    image: str = DEFAULT_IMAGE
    runner: Callable[..., bytes] = run_docker
    name: str = field(default_factory=lambda: f"pi_code_agent-s13-a-{uuid4().hex}", init=False)
    _created: bool = field(default=False, init=False)
    _ready: bool = field(default=False, init=False)

    def start(self, snapshot: WorkspaceSnapshot, run_root: Path) -> None:
        """创建没有宿主挂载的 Linux 容器，复制过滤后的源码。

        输入：可信启动快照、仅 Harness 写入的运行目录；输出：就绪容器和 source.tar。
        流程：确认本地镜像并固定 image ID → 创建受限容器 → 复制 tar → 解压副本。
        边界：只使用本地镜像，不自动 pull/build；任何步骤失败由调用方 finally 清理。
        """
        if self._created:
            raise SandboxError("同一执行器不能重复创建容器")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", self.image):
            raise SandboxError("镜像名称无效")
        metadata = (
            self.runner(["docker", "image", "inspect", "--format", "{{.Id}} {{.Os}}", self.image])
            .decode()
            .strip()
            .split()
        )
        if (
            len(metadata) != 2
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata[0])
            or metadata[1] != "linux"
        ):
            raise SandboxError("需要已准备的可信 Linux 镜像；程序不会自动拉取")

        run_root.mkdir(parents=True, exist_ok=False)
        archive_path = run_root / "source.tar"
        snapshot.write_tar(archive_path)
        # s13-A 新增：启动参数全部来自 Harness；无挂载、无端口、无宿主环境透传。
        arguments = [
            "docker",
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            self.name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65534:65534",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/work:rw,size=512m,mode=1777",
            "--tmpfs",
            "/tmp:rw,size=128m,mode=1777",
            "--workdir",
            "/work",
            "--init",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--entrypoint",
            "python",
            metadata[0],
            "-I",
            "-c",
            "import time; time.sleep(86400)",
        ]
        # s13-A 新增：即使启动返回失败，也只按本次随机名称清理可能创建的容器。
        self._created = True
        self.runner(arguments)
        # s13-A 新增：tar 经 stdin 交给非 root Worker 解压，避免 docker cp 的 root 属主。
        self._request({"action": "init"}, archive=archive_path.read_bytes())
        self._ready = True

    def _request(self, request: dict[str, Any], *, archive: bytes | None = None) -> dict[str, Any]:
        """固定 Worker 的 stdin：启动时为源码 tar，工具调用时为 JSON；不拼接命令。"""
        data = (
            archive
            if archive is not None
            else json.dumps(request, ensure_ascii=False).encode("utf-8")
        )
        if archive is None and len(data) > 128 * 1024:
            raise ValueError("单次工具参数超过 128 KiB")
        arguments = [
            "docker",
            "exec",
            "--interactive",
            "--workdir",
            "/work",
            self.name,
            "python",
            "-I",
            WORKER,
        ]
        # s13-A 新增：初始化只接收宿主生成的 tar，不作为模型工具开放。
        if archive is not None:
            arguments.append("--init")
        output = self.runner(arguments, data=data, timeout=135)
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError) as error:
            raise SandboxError("容器未返回有效 JSON；不能继续执行") from error
        if not isinstance(response, dict) or response.get("fatal"):
            raise SandboxError(
                str(response.get("fatal")) if isinstance(response, dict) else "容器响应无效"
            )
        return response

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """把已注册工具交给容器执行，不做逐条宿主审批。

        输入：模型工具名与参数；输出：工具结果，普通参数错误可反馈给模型。
        流程：检查就绪与工具集合 → 固定 Docker exec → 校验 Worker 输出。
        边界：Docker、Worker 或超时失败会关闭就绪状态并抛出 SandboxError。
        """
        if not self._ready:
            raise SandboxError("Sandbox 未就绪；禁止宿主回退")
        if name not in TOOL_NAMES:
            return f"Permission denied: unknown tool {name}"
        try:
            response = self._request({"action": "tool", "name": name, "arguments": arguments})
            if not isinstance(response.get("output"), str):
                raise SandboxError("容器工具结果不是文本")
            return response["output"]
        except ValueError as error:
            return f"Error: {error}"
        except SandboxError:
            self._ready = False
            raise

    # s13-A 新增：导出与比较分离，让预览和应用共享同一个经过校验的文件快照。
    def capture_workspace(self) -> WorkspaceSnapshot:
        """获取容器当前文件内容，不在宿主解压或写回文件。

        输入：已就绪容器；输出：校验过路径、数量和字节预算的 WorkspaceSnapshot。
        流程：请求固定 Worker 的 snapshot → 校验导出映射 → 返回内存快照。
        边界：容器或协议失败即禁止继续工具执行；回写权限不属于本方法。
        """
        if not self._ready:
            raise SandboxError("Sandbox 已失败，未导出最终修改；原仓库未被工具写入")
        try:
            response = self._request({"action": "snapshot"})
            return WorkspaceSnapshot.from_payload(response.get("files"))
        except SandboxError:
            self._ready = False
            raise
        except (ValueError, TypeError) as error:
            self._ready = False
            raise SandboxError(f"容器导出校验失败：{error}") from error

    def close(self) -> None:
        """移除本次唯一名称的容器，终止其中所有剩余进程；失败明确报告。"""
        self._ready = False
        if self._created:
            self.runner(["docker", "rm", "--force", self.name])
            self._created = False
