"""s13-A 新增：容器内执行器；模型工具只走 JSON，不让模型接触 Docker 参数。"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

# s13-A 新增：-I 忽略项目 PYTHONPATH，只加载镜像内只读的 Harness 代码。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tutorials.s13_a_sandbox_copy.snapshot import WorkspaceSnapshot


def run_bash(command: str) -> str:
    """在容器 /work 中运行 Linux shell，不继承宿主 shell 或凭证。

    输入：命令文本；输出：有界结果和退出状态；超时向宿主报告致命失败。
    流程：执行 /bin/sh → 等待结果 → 复用 s02 的末尾预算与容器内全文存档。
    边界：文件和网络限制由 Docker 提供；超时后宿主关闭整个容器，清理子进程。
    """
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
    output = base_tools._limit_shell_output((result.stdout + result.stderr).strip(), Path("/work"))
    if result.returncode:
        return f"Error: command exited with code {result.returncode}\n{output}"
    return output


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理一次宿主请求，普通工具错误可反馈，基础设施错误不可继续。

    输入：固定 action、工具名称和参数；输出：可编码成 JSON 的结果。
    流程：导出快照 / 查注册表执行工具；不会执行宿主指令。
    边界：snapshot 仅由 Harness 使用，不作为模型工具开放。
    """
    action = request["action"]
    if action == "snapshot":
        return {"files": WorkspaceSnapshot.capture(Path("/work")).to_payload()}
    if action != "tool":
        raise ValueError("未知执行请求")

    import base_tools

    # 来自 s02：保持；文件工具的路径检查、输出预算和 handler 都复用只读镜像代码。
    handlers = dict(base_tools.TOOL_HANDLERS)
    # s13-A 修改：只有 bash 换成容器 shell；不是换到宿主无审批执行。
    handlers["bash"] = run_bash
    name = request["name"]
    handler = handlers.get(name)
    if handler is None:
        return {"output": f"Permission denied: unknown tool {name}"}
    try:
        return {"output": handler(**request["arguments"])}
    except subprocess.TimeoutExpired:
        return {"fatal": "容器命令超过 120 秒；必须关闭容器，不能继续执行"}
    except (TypeError, ValueError, OSError) as error:
        return {"output": f"Error: {error}"}


def main() -> None:
    """启动时读取源码 tar，运行时读取工具 JSON；统一返回 JSON，失败标记 fatal。"""
    try:
        # s13-A 新增：由非 root 用户解压 stdin，副本自然归该用户所有，文件保持可写。
        if sys.argv[1:] == ["--init"]:
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
                archive.extractall("/work", filter="data")
            response = {"output": "工作区副本已就绪"}
        else:
            request = json.loads(sys.stdin.read())
            response = handle_request(request)
    except Exception as error:  # noqa: BLE001 - Worker 的异常必须编码成 fatal，不能继续执行
        response = {"fatal": f"容器执行器失败：{type(error).__name__}: {error}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
