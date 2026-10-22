"""s13-B 新增：固定工具在只读镜像中，所有文件操作面向共享 /work。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tutorials.s13_b_sandbox_workspace.workspace_access import check_file_arguments


def run_bash(command: str) -> str:
    """在容器内执行 Linux shell；全文存档到 /tmp，不写宿主运行记录目录。"""
    import base_tools

    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd="/work",
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )
    output = base_tools._limit_shell_output((result.stdout + result.stderr).strip(), Path("/tmp"))
    if result.returncode:
        return f"Error: command exited with code {result.returncode}\n{output}"
    return output


def handle_request(request: dict) -> dict:
    """探测固定 Worker 或执行注册工具；路径检查不代替容器挂载限制。"""
    if request["action"] == "probe":
        if os.getuid() == 0 or os.access("/opt/pi-code-agent", os.W_OK):
            return {"fatal": "容器必须非 root 且固定工具目录只读"}
        return {"output": "ready"}
    if request["action"] != "tool":
        raise ValueError("未知 Worker action")
    import base_tools

    name, arguments = request["name"], request["arguments"]
    denial = check_file_arguments(name, arguments, Path("/work"))
    if denial:
        return {"output": denial}
    # 来自 s02：保持；专用工具实现仍从只读镜像加载。
    handlers = dict(base_tools.TOOL_HANDLERS)
    handlers["bash"] = run_bash
    if name not in handlers:
        return {"output": "Permission denied: unknown tool"}
    return {"output": handlers[name](**arguments)}


def main() -> None:
    """读取 stdin 工具 JSON，返回结果；超时和基础设施错误标为 fatal。"""
    try:
        response = handle_request(json.loads(sys.stdin.read()))
    except (TypeError, ValueError) as error:
        response = {"output": f"Error: {error}"}
    except Exception as error:  # noqa: BLE001 - 不允许 Worker 异常后回退到宿主
        response = {"fatal": f"共享 Worker 失败：{error}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
