"""s13-B 新增：受限容器共享真实项目；不复制源码、不确认补丁交付。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pi_code_agent.sandbox_copy.sandbox import TOOL_NAMES, SandboxError, run_docker
from pi_code_agent.sandbox_shared.workspace_access import inspect_workspace

DEFAULT_IMAGE = "pi-code-agent-sandbox:shared"
WORKER = "/opt/pi-code-agent/src/pi_code_agent/sandbox_shared/tool_worker.py"


# s13-B 新增：执行器继续使用 A 的固定 Docker argv/JSON 思路，文件位置改为共享挂载。
@dataclass
class SharedWorkspaceExecutor:
    """在受限容器中执行六种工具，修改立即影响指定宿主项目。

    输入：用户授权的工作区、可信镜像、Docker 出口；输出：工具结果和即时文件修改。
    流程：start 检查路径并挂载 → dispatch 通过固定 Worker 执行 → close 清理容器。
    边界：禁网、非 root、只读根；保护路径用只读空对象遮蔽，不把模型命令交给宿主。
    与 A 不同：没有 /apply，也不能阻止允许范围内的误删；失败不回退宿主。
    """

    workspace: Path
    image: str = DEFAULT_IMAGE
    runner: Callable[..., bytes] = run_docker
    name: str = field(default_factory=lambda: f"pi_code_agent-s13-b-{uuid4().hex}", init=False)
    _created: bool = field(default=False, init=False)
    _ready: bool = field(default=False, init=False)
    _protected: tuple = field(default=(), init=False, repr=False)

    def start(self, run_root: Path) -> None:
        """准备只读遮蔽物，创建直接连接真实工作区的容器并验证 Worker。

        输入：宿主报告目录；输出：就绪执行器和 protection.json。
        流程：验证镜像 → 检查工作区 → 创建空遮蔽物 → 固定挂载启动 → 探测。
        遮蔽物只写到报告目录；不 chmod 项目、不删除项目凭证或 Git 元数据。
        """
        if self._created:
            raise SandboxError("执行器不能重复启动")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", self.image):
            raise SandboxError("镜像名称无效")
        # s13-B 新增：共享路径属于本机，不能误把同名路径挂到远程 Docker 主机。
        try:
            context = json.loads(self.runner(["docker", "context", "inspect"]))
            endpoint = context[0]["Endpoints"]["docker"]["Host"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise SandboxError("无法确认 Docker 本机连接") from error
        if not os.environ.get("DOCKER_CONTEXT"):
            endpoint = os.environ.get("DOCKER_HOST") or endpoint
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
            raise SandboxError("共享工作区只支持本机 Docker 引擎，不支持 TCP/SSH 远程上下文")
        metadata = (
            self.runner(["docker", "image", "inspect", "--format", "{{.Id}} {{.Os}}", self.image])
            .decode()
            .strip()
            .split()
        )
        if (
            len(metadata) != 2
            or metadata[1] != "linux"
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata[0])
        ):
            raise SandboxError("需要手动准备的可信 Linux 镜像；不会自动下载")
        run_root.mkdir(parents=True, exist_ok=False)
        self._protected = inspect_workspace(self.workspace)
        # s13-B 新增：不把宿主路径拼进 shell；逗号会改变 --mount 字段，提前拒绝。
        if any(char in str(path) for path in (self.workspace, run_root) for char in ",\r\n"):
            raise SandboxError("Docker 挂载路径不能包含逗号或换行")
        uid = os.getuid() if hasattr(os, "getuid") else 65534
        gid = os.getgid() if hasattr(os, "getgid") else 65534
        if uid == 0:
            raise SandboxError("请用普通宿主用户运行共享工作区，不能以 root 放权")
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
            f"{uid}:{gid}",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,size=128m,mode=1777",
            "--workdir",
            "/work",
            "--init",
            "--log-driver",
            "none",
            "--no-healthcheck",
            # s13-B 修改：/work 是真实项目，不再使用 tmpfs 或 tar 初始化。
            "--mount",
            f"type=bind,source={self.workspace},target=/work,bind-recursive=disabled",
        ]
        for index, item in enumerate(self._protected):
            if any(char in item.relative for char in ",\r\n"):
                raise SandboxError("保护路径不能包含逗号或换行")
            mask = run_root / "masks" / str(index)
            mask.parent.mkdir(parents=True, exist_ok=True)
            if item.is_directory:
                mask.mkdir()
            else:
                mask.touch()
            # s13-B 新增：只读子挂载也阻止删除/替换挂载点；必须由真实 Docker 测试验证。
            arguments += [
                "--mount",
                f"type=bind,source={mask},target=/work/{item.relative},readonly",
            ]
        (run_root / "protection.json").write_text(
            json.dumps([item.relative for item in self._protected], ensure_ascii=False),
            encoding="utf-8",
        )
        arguments += [
            "--entrypoint",
            "python",
            metadata[0],
            "-I",
            "-c",
            "import time; time.sleep(86400)",
        ]
        self._created = True
        self.runner(arguments)
        response = self._request({"action": "probe"})
        if response.get("output") != "ready":
            raise SandboxError("共享容器探测失败")
        self._ready = True

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        """固定 Worker argv，工具名和模型参数只进入 stdin JSON。"""
        data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(data) > 128 * 1024:
            raise ValueError("单次工具参数超过 128 KiB")
        output = self.runner(
            [
                "docker",
                "exec",
                "--interactive",
                "--workdir",
                "/work",
                self.name,
                "python",
                "-I",
                WORKER,
            ],
            data=data,
            timeout=135,
        )
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError) as error:
            raise SandboxError("Worker 未返回有效 JSON") from error
        if not isinstance(response, dict) or response.get("fatal"):
            raise SandboxError(
                str(response.get("fatal")) if isinstance(response, dict) else "Worker 响应无效"
            )
        return response

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """重核工作区，再将工具交给共享容器；不逐条确认，不走宿主 handler。

        输入：工具名和参数；输出：工具文本，允许文件的修改已经写入宿主。
        流程：检查就绪 → 检查链接及保护清单没有变化 → Worker 执行 → 验证结果。
        致命错误关闭就绪状态，后续调用拒绝；普通参数错误只反馈模型。
        """
        if not self._ready:
            raise SandboxError("共享 Sandbox 未就绪；禁止宿主回退")
        if name not in TOOL_NAMES:
            return "Permission denied: unknown tool"
        try:
            if inspect_workspace(self.workspace) != self._protected:
                raise SandboxError("工作区保护路径已变化；请停止并重新启动")
            response = self._request({"action": "tool", "name": name, "arguments": arguments})
            if not isinstance(response.get("output"), str):
                raise SandboxError("Worker 结果不是文本")
            return response["output"]
        except ValueError as error:
            return f"Error: {error}"
        except (SandboxError, OSError) as error:
            self._ready = False
            raise SandboxError(str(error)) from error

    def close(self) -> None:
        """只移除本次容器；共享项目文件已经生效，不能声称退出会撤销修改。"""
        self._ready = False
        if self._created:
            self.runner(["docker", "rm", "--force", self.name])
            self._created = False
