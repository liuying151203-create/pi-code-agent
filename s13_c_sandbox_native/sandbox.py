"""s13-C 新增：Python Harness 调用固定版本 srt CLI，不把模型命令交给宿主 shell。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from s13_a_sandbox_copy.sandbox import TOOL_NAMES, SandboxError, _send_input
from s13_b_sandbox_workspace.workspace_access import inspect_workspace

RUNTIME_VERSION = "0.0.79"
MAX_OUTPUT = 20 * 1024 * 1024


def _terminate_tree(process: subprocess.Popen) -> None:
    """只终止当前启动器的进程树，不按名称杀掉其他 Python、Node 或服务。"""
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
        subprocess.run(
            [str(system_root / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _read_pipe(pipe, output: bytearray, exceeded: Event, process: subprocess.Popen) -> None:
    """有界读取 stdout/stderr，输出过大即终止本次启动器。"""
    while chunk := pipe.read(65536):
        room = MAX_OUTPUT - len(output)
        output.extend(chunk[:room])
        if len(chunk) > room:
            exceeded.set()
            _terminate_tree(process)
            return


def run_native(arguments: list[str], *, data: bytes, cwd: Path, timeout: int = 135) -> bytes:
    """运行固定 Node/srt argv，清除密钥环境，等待有界 JSON 结果。

    输入：Harness 构造的命令、工作区、stdin；输出：stdout，stderr 仅用于失败诊断。
    流程：白名单环境 → 无宿主 shell 的启动器 → 并行读写管道 → 超时清理进程树。
    这不是沙箱本身；只有 srt 应用 OS 策略并通过探测，执行器才允许模型调用。
    """
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
    except OSError as error:
        raise SandboxError("无法启动原生沙箱；请按 README 手动准备 Node 和 runtime") from error
    stdout, stderr, exceeded = bytearray(), bytearray(), Event()
    readers = [
        Thread(target=_read_pipe, args=(process.stdout, stdout, exceeded, process), daemon=True),
        Thread(target=_read_pipe, args=(process.stderr, stderr, exceeded, process), daemon=True),
    ]
    for reader in readers:
        reader.start()
    Thread(target=_send_input, args=(process, data), daemon=True).start()
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        _terminate_tree(process)
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SandboxError(f"原生沙箱超过 {timeout} 秒；停止本次任务") from error
    finally:
        for reader in readers:
            reader.join(timeout=2)
    if exceeded.is_set() or any(reader.is_alive() for reader in readers):
        _terminate_tree(process)
        raise SandboxError("原生沙箱输出超限或子进程未关闭管道")
    process.stdout.close()
    process.stderr.close()
    if process.returncode:
        diagnostic = bytes(stderr[:2000]).decode("utf-8", errors="replace")
        raise SandboxError(f"原生沙箱执行失败：{diagnostic}")
    return bytes(stdout)


@dataclass
class NativeSandboxExecutor:
    """通过固定版本 sandbox-runtime 包住全部工具 Worker，不依赖 Docker。

    输入：目标项目、Harness、用户准备的 runtime 目录、可替换进程出口。
    输出：受限工具结果和直接写入的项目文件，策略保存到报告目录。
    流程：start 准备固定工具和策略、验证隔离 → dispatch 启动受限 Worker → close。
    边界：不会安装或提权；Windows 初始化必须由用户完成，失败绝不普通本地回退。
    """

    workspace: Path
    harness_root: Path
    runtime_root: Path
    runner: Callable[..., bytes] = run_native
    name: str = field(default_factory=lambda: f"zero2pi-s13-c-{uuid4().hex}", init=False)
    _ready: bool = field(default=False, init=False)
    _run_root: Path | None = field(default=None, init=False)
    _temporary: tempfile.TemporaryDirectory | None = field(default=None, init=False, repr=False)
    _arguments: list[str] = field(default_factory=list, init=False, repr=False)
    _protected: tuple = field(default=(), init=False, repr=False)

    def start(self, run_root: Path) -> None:
        """检查 runtime，固定工具代码，生成限制并实际探测后才允许执行。

        输入：新报告目录；输出：settings.json、trusted 固定工具和就绪状态。
        工作区可写，记录目录只读且秘密不可读；临时工具输出有独立可写目录。
        探测会检测错误放权，但不能代替对 runtime 的信任或平台安全审计。
        """
        if self._run_root is not None:
            raise SandboxError("原生执行器不能重复启动")
        if sys.platform not in {"win32", "linux", "darwin"}:
            raise SandboxError("原生沙箱不支持当前平台")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise SandboxError("原生沙箱请用普通用户运行，不接受 root 放权")
        node = shutil.which("node")
        try:
            runtime = self.runtime_root.resolve(strict=True)
            manifest = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SandboxError("原生 runtime 未准备好；请按 README 手动安装固定版本") from error
        if not isinstance(manifest, dict):
            raise SandboxError("runtime package.json 无效")
        if (
            not node
            or manifest.get("name") != "@anthropic-ai/sandbox-runtime"
            or manifest.get("version") != RUNTIME_VERSION
        ):
            raise SandboxError(f"请手动准备 Node >=22.12 和 sandbox-runtime {RUNTIME_VERSION}")
        cli = runtime / "dist/cli.js"
        if not cli.is_file():
            raise SandboxError("runtime 缺少 dist/cli.js；不会自动下载或构建")
        run_root.mkdir(parents=True, exist_ok=False)
        self._run_root = run_root
        self._protected = inspect_workspace(self.workspace)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="zero2pi-s13-c-", dir=Path(tempfile.gettempdir()).resolve()
        )
        output_root = Path(self._temporary.name).resolve()
        trusted = run_root / "trusted"
        trusted.mkdir()
        # s13-C 新增：不是导入可写项目代码；工具快照放在本次 OS 策略保护的目录。
        sources = {
            "s02_tool_use/code.py": "base_tools.py",
            "s13_c_sandbox_native/tool_worker.py": "tool_worker.py",
            "s13_b_sandbox_workspace/__init__.py": "s13_b_sandbox_workspace/__init__.py",
            "s13_b_sandbox_workspace/workspace_access.py": "s13_b_sandbox_workspace/workspace_access.py",
            "s13_a_sandbox_copy/__init__.py": "s13_a_sandbox_copy/__init__.py",
            "s13_a_sandbox_copy/sandbox.py": "s13_a_sandbox_copy/sandbox.py",
            "s13_a_sandbox_copy/snapshot.py": "s13_a_sandbox_copy/snapshot.py",
        }
        for path in (self.harness_root / "src/zero2pi").glob("*.py"):
            sources[path.relative_to(self.harness_root).as_posix()] = f"zero2pi/{path.name}"
        for source, destination in sources.items():
            target = trusted / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.harness_root / source).read_bytes())
        deny_read = [self.harness_root / ".env", run_root, Path.home(), Path(tempfile.gettempdir())]
        deny_write = [run_root, runtime]
        for item in self._protected:
            # s13-C 修改：解释器依赖可以读取但不能修改；凭证和元数据同时禁止读取。
            if item.path.name.lower() not in {".venv", "venv", "node_modules"}:
                deny_read.append(item.path)
            deny_write.append(item.path)
        literal = lambda path: {"path": str(path), "literal": True}
        settings = {
            "network": {"allowedDomains": [], "deniedDomains": []},
            "filesystem": {
                "denyRead": [literal(path) for path in deny_read],
                "allowRead": [
                    literal(path)
                    for path in (
                        self.workspace,
                        trusted,
                        output_root,
                        runtime,
                        Path(sys.prefix),
                        Path(sys.base_prefix),
                    )
                ],
                "allowWrite": [literal(self.workspace), literal(output_root)],
                "denyWrite": [literal(path) for path in deny_write],
            },
        }
        settings_path = run_root / "settings.json"
        settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        worker_argv = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(trusted / "tool_worker.py"),
            str(output_root),
        ]
        if os.name == "nt":
            # s13-C 新增：仅固定 Worker 路径进入 cmd 字符串，模型命令仍只进入 stdin。
            if any(char in value for value in worker_argv for char in '%!&|<>^"\r\n'):
                raise SandboxError("Windows 固定 Worker 路径含 shell 特殊字符，拒绝启动")
            # s13-C 新增：cmd /s /c 会剥掉最外层引号，额外包一层保留带空格的路径。
            command = '"' + " ".join(f'"{value}"' for value in worker_argv) + '"'
        else:
            command = shlex.join(worker_argv)
        self._arguments = [node, str(cli), "--settings", str(settings_path), "-c", command]
        secret = run_root / "probe-secret.txt"
        secret.write_bytes(b"native-probe-secret")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            if 60080 <= port <= 60089:
                raise SandboxError("探测端口落入 runtime 代理范围，请重新启动")
            response = self._request(
                {
                    "action": "probe",
                    "secret": str(secret),
                    "forbidden_write": str(run_root / "probe-forbidden.txt"),
                    "port": port,
                }
            )
        if response.get("output") != "ready":
            raise SandboxError("原生沙箱探测失败")
        self._ready = True

    def _request(self, request: dict[str, Any]) -> dict:
        """固定 runtime argv，只通过 stdin 传入工具参数，任何协议异常都停止。"""
        data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(data) > 128 * 1024:
            raise ValueError("单次工具参数超过 128 KiB")
        output = self.runner(self._arguments, data=data, cwd=self.workspace, timeout=135)
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError) as error:
            raise SandboxError("原生 Worker 未返回有效 JSON") from error
        if not isinstance(response, dict) or response.get("fatal"):
            raise SandboxError(
                str(response.get("fatal")) if isinstance(response, dict) else "Worker 响应无效"
            )
        return response

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """检查工作区未被换成链接，调用受限 Worker，修改立即生效。

        输入：模型工具名/参数；输出：结果文本；未知工具拒绝，不扩大可写目录。
        流程：检查就绪和保护清单 → srt 应用策略 → Worker 执行 → 返回结果。
        致命失败撤销就绪状态；原始会话由核心循环补齐工具结果后保存。
        """
        if not self._ready:
            raise SandboxError("原生 Sandbox 未就绪；禁止普通本地回退")
        if name not in TOOL_NAMES:
            return "Permission denied: unknown tool"
        try:
            if inspect_workspace(self.workspace) != self._protected:
                raise SandboxError("工作区保护路径已变化；请停止并重新启动")
            response = self._request({"action": "tool", "name": name, "arguments": arguments})
            if not isinstance(response.get("output"), str):
                raise SandboxError("原生工具结果不是文本")
            return response["output"]
        except ValueError as error:
            return f"Error: {error}"
        except (SandboxError, OSError) as error:
            self._ready = False
            raise SandboxError(str(error)) from error

    def close(self) -> None:
        """结束执行器并清理本次临时输出；项目修改和宿主报告保留。"""
        self._ready = False
        if self._temporary is not None:
            # tempfile 管理本程序创建的唯一目录，不删除工作区或其他 runtime 目录。
            path = Path(self._temporary.name)
            if path.is_symlink() or path.is_junction() or path.resolve() != path.absolute():
                raise SandboxError("本次临时目录被重定向；拒绝递归清理，请人工检查")
            self._temporary.cleanup()
            self._temporary = None
