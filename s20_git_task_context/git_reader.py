"""s20：只读取 Git 对象/引用，不对工作树运行 diff、status 或仓库脚本。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from threading import Event, Thread

MAX_GIT_BYTES = 2 * 1024 * 1024


def _drain(stream, output: bytearray, exceeded: Event) -> None:
    """持续排空有界管道；超限仍排空，主线程检测后终止进程。"""
    while chunk := stream.read(65536):
        available = MAX_GIT_BYTES - len(output)
        output.extend(chunk[:available])
        if len(chunk) > available:
            exceeded.set()


# s20 新增：参考 Pi 将能力留在组件；Git 读取不变成可任意拼接的 shell 工具。
class GitReader:
    """为选定根目录读取有限 Git 元数据和 blob。

    输入：目标路径及组件内部构造的 argv；输出：有界 bytes。
    流程：清除继承 Git 变量 → 禁用外部辅助机制 → shell=False 读取 → 超限/超时停止。
    边界：公开方法不接收 shell；无网络、写入、过滤器、钩子或隐式向父项目取上下文。
    """

    def __init__(self, root: Path) -> None:
        """固定 cwd；不启动 Git、不写配置。"""
        self.root = root

    def read(self, arguments: list[str], *, allow_failure: bool = False) -> bytes | None:
        """执行固定只读命令，保持输出和时间预算。

        输入：调用组件构造的命令参数；输出：stdout，允许的缺失返回 None。
        流程：校验命令白名单 → 准备干净环境 → 并行读管道 → 限时等待 → 核对退出状态。
        边界：不把配置错误静默变为空历史；allow_failure 仅用于探测非 Git/未提交状态。
        """
        if not arguments or arguments[0] not in {
            "rev-parse",
            "symbolic-ref",
            "log",
            "ls-tree",
            "cat-file",
        }:
            raise ValueError("只允许固定 Git 只读命令")
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_LITERAL_PATHSPECS="1",
            # s20 新增：部分克隆缺对象时明确失败，禁止读取触发隐式 fetch/传输程序。
            GIT_NO_LAZY_FETCH="1",
            GIT_ALLOW_PROTOCOL="",
        )
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            # s20 新增：不让仓库 log 配置启动外部签名验证程序。
            "-c",
            "log.showSignature=false",
            *arguments,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=environment,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise RuntimeError("无法读取 Git；请手动准备 Git，程序不自动安装") from error
        stdout, stderr, exceeded = bytearray(), bytearray(), Event()
        readers = [
            Thread(target=_drain, args=(stream, buffer, exceeded), daemon=True)
            for stream, buffer in ((process.stdout, stdout), (process.stderr, stderr))
        ]
        for reader in readers:
            reader.start()
        try:
            # s20 新增：短间隔检查输出上限；不等到超时才终止失控输出。
            for _ in range(200):
                try:
                    process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if exceeded.is_set():
                        raise RuntimeError("Git 输出超过 2 MiB，任务上下文未构建")
            else:
                raise RuntimeError("Git 读取超过 10 秒")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for reader in readers:
                reader.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
        if exceeded.is_set() or any(reader.is_alive() for reader in readers):
            raise RuntimeError("Git 输出超限或管道未关闭")
        if process.returncode:
            if allow_failure:
                return None
            raise RuntimeError(f"Git {arguments[0]} 读取失败，未使用不完整上下文")
        return bytes(stdout)
