"""s21：固定容器 Worker，验证命令与源码导出均非模型工具。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from s13_a_sandbox_copy import tool_worker as previous
from s21_sandbox_repair.worker_snapshot import capture_project, source_files


# s21 新增：退出码来自受信任父 Worker 的进程对象，不从模型描述或输出文本推断。
def run_verification(argv: list[str]) -> dict:
    """在容器内执行宿主用户选定的验证命令。

    输入：固定 argv；输出：真实退出码、有界日志和省略标志。
    流程：shell=False 启动 → 限时/限输出等待 → 获取退出码 → 返回末尾日志。
    边界：该 action 不在模型目录；超时/超输出为 fatal，宿主随后关闭整个容器。
    """
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 32
        or any(
            not isinstance(arg, str) or not arg or len(arg) > 1000 or "\0" in arg for arg in argv
        )
    ):
        raise ValueError("验证命令必须为 1–32 个有效 argv")
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            argv,
            cwd="/work",
            env=environment,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(1200):
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if os.fstat(output.fileno()).st_size > 65536:
                        raise RuntimeError("验证日志超过 64 KiB，必须停止容器")
            else:
                raise RuntimeError("验证超过 120 秒，必须停止容器")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        size = os.fstat(output.fileno()).st_size
        output.seek(max(0, size - 16000))
        text = output.read(16000).decode("utf-8", errors="replace")
    if size > 65536:
        raise RuntimeError("验证日志超过 64 KiB，必须停止容器")
    return {"exit_code": process.returncode, "output": text, "truncated": size > 16000}


def handle_request(request: dict) -> dict:
    """分开处理业务快照、检索视图、验证和原有容器工具，不开放宿主能力。"""
    action = request.get("action")
    if action in {"snapshot", "source"}:
        snapshot = capture_project(Path("/work"))
        if action == "source":
            snapshot = source_files(snapshot, Path("/work"))
        return {"files": snapshot.to_payload()}
    if action == "verify":
        return run_verification(request.get("argv"))
    return previous.handle_request(request)


def main() -> None:
    """沿用 s13-A 的可信 tar 初始化；运行请求统一 JSON，异常必须 fatal。"""
    if sys.argv[1:] == ["--init"]:
        previous.main()
        return
    try:
        response = handle_request(json.loads(sys.stdin.read()))
    except Exception as error:  # noqa: BLE001 - Worker 失败必须阻断宿主继续执行
        response = {"fatal": f"s21 Worker 失败：{type(error).__name__}: {error}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
