"""s13-C 新增：所有六种工具在受限 Python 子进程执行，不在宿主分发。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

# s13-C 新增：执行器把固定工具复制到只读策略保护的目录，不导入可写项目的实现。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tutorials.s13_b_sandbox_workspace.workspace_access import check_file_arguments


def probe(request: dict) -> dict:
    """验证保护文件不可读、报告目录不可写、随机宿主监听端口不可连接。"""
    secret = Path(request["secret"])
    try:
        if secret.read_bytes() == b"native-probe-secret":
            return {"fatal": "原生沙箱未阻止受保护文件读取"}
    except (OSError, ValueError):
        pass
    try:
        Path(request["forbidden_write"]).write_text("unconfined", encoding="utf-8")
        return {"fatal": "原生沙箱未阻止工作区外写入"}
    except OSError:
        pass
    try:
        with socket.create_connection(("127.0.0.1", request["port"]), timeout=1):
            return {"fatal": "原生沙箱未阻止网络连接"}
    except OSError:
        pass
    return {"output": "ready"}


def run_bash(command: str, output_root: Path) -> str:
    """在原生沙箱内部执行平台 shell；只把工具输出存入本次允许的临时目录。"""
    import base_tools

    shell = (
        [
            str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/cmd.exe"),
            "/d",
            "/s",
            "/c",
        ]
        if os.name == "nt"
        else ["/bin/sh", "-c"]
    )
    result = subprocess.run(
        [*shell, command],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )
    output = base_tools._limit_shell_output((result.stdout + result.stderr).strip(), output_root)
    if result.returncode:
        return f"Error: command exited with code {result.returncode}\n{output}"
    return output


def handle_request(request: dict, output_root: Path) -> dict:
    """处理固定探测或模型工具；原生限制由启动器包住整个 Worker 及子进程。"""
    if request["action"] == "probe":
        return probe(request)
    if request["action"] != "tool":
        raise ValueError("未知 Worker action")
    import base_tools

    name, arguments = request["name"], request["arguments"]
    denial = check_file_arguments(name, arguments, Path.cwd().resolve())
    if denial:
        return {"output": denial}
    if name == "bash":
        return {"output": run_bash(arguments["command"], output_root)}
    handler = base_tools.TOOL_HANDLERS.get(name)
    if handler is None:
        return {"output": "Permission denied: unknown tool"}
    return {"output": handler(**arguments)}


def main() -> None:
    """以 UTF-8 接收 stdin JSON，只输出结果 JSON；基础设施错误必须停止。"""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        # s13-C 新增：输出目录由固定启动 argv 提供，模型不能扩展可写范围。
        response = handle_request(json.loads(sys.stdin.read()), Path(sys.argv[1]))
    except (TypeError, ValueError) as error:
        response = {"output": f"Error: {error}"}
    except Exception as error:  # noqa: BLE001 - 超时或执行环境异常必须停止，不回退
        response = {"fatal": f"原生 Worker 失败：{error}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
