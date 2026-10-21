"""s21：复用 A 隔离参数，仅增加固定 Worker 的源码/验证协议。"""

import json
from dataclasses import dataclass

from s13_a_sandbox_copy.sandbox import SandboxError
from s13_a_sandbox_copy.sandbox import SandboxExecutor as CopyExecutor
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot

DEFAULT_IMAGE = "zero2pi-sandbox:s21"
WORKER = "/opt/zero2pi/s21_sandbox_repair/tool_worker.py"


@dataclass
class SandboxExecutor(CopyExecutor):
    """保留无网络、无宿主挂载的执行边界，提供同源检索与验证。

    输入：已有本地 s21 镜像；输出：普通工具结果、当前源码和验证退出码。
    流程：父类 start → 新 Worker 请求 → 导出/验证 → 父类 close。
    边界：不在宿主运行测试，不自动 pull/build；任一协议失败禁止后续执行。
    """

    # s21 修改：直接构造也使用 s21 镜像，不依赖 main 恰好覆盖父类默认值。
    image: str = DEFAULT_IMAGE

    @property
    def is_ready(self) -> bool:
        """供入口判断能否做最终预览，不暴露父类内部状态字段。"""
        return self._ready

    def ensure_ready(self) -> None:
        """让运行时在检索错误包装后仍能发现致命后端故障。"""
        if not self._ready:
            raise SandboxError("s21 容器不可用，已停止；禁止宿主回退")

    def fail(self, reason: str) -> None:
        """协议校验失败后永久禁用本实例，不能把故障当普通工具错误重试。"""
        self._ready = False
        raise SandboxError(reason)

    # s21 修改：覆盖小的协议边界，不修改 s13-A 全局 WORKER 或隔离启动参数。
    def _request(self, request: dict, *, archive: bytes | None = None) -> dict:
        """只将固定 Worker 路径换成 s21；模型无法选择 action 或 Docker 参数。"""
        data = archive if archive is not None else json.dumps(request, ensure_ascii=False).encode()
        if archive is None and len(data) > 128 * 1024:
            raise ValueError("单次请求超过 128 KiB")
        argv = [
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
        if archive is not None:
            argv.append("--init")
        try:
            output = self.runner(argv, data=data, timeout=135)
            response = json.loads(output)
            if not isinstance(response, dict) or response.get("fatal"):
                raise ValueError(
                    response.get("fatal") if isinstance(response, dict) else "响应不是对象"
                )
            return response
        except (ValueError, TypeError, SandboxError) as error:
            self._ready = False
            raise SandboxError(f"s21 执行器失败，禁止宿主回退：{error}") from error

    def capture_source(self) -> WorkspaceSnapshot:
        """获取容器当前允许源码，供检索和读取共同使用；不读取宿主原代码。"""
        if not self._ready:
            raise SandboxError("容器未就绪")
        try:
            return WorkspaceSnapshot.from_payload(self._request({"action": "source"}).get("files"))
        except (ValueError, TypeError) as error:
            self._ready = False
            raise SandboxError("容器源码视图无效") from error

    def verify(self, argv: list[str]) -> dict:
        """执行 Harness 持有的验证配置；退出状态必须是协议中的整数，不解析日志猜测。"""
        if not self._ready:
            raise SandboxError("容器未就绪")
        result = self._request({"action": "verify", "argv": argv})
        if (
            type(result.get("exit_code")) is not int
            or not isinstance(result.get("output"), str)
            or type(result.get("truncated")) is not bool
            or len(result["output"].encode()) > 65536
        ):
            self._ready = False
            raise SandboxError("验证响应结构无效")
        return result
