"""s03：组装配置、模型和只读工具，终端与 SDK 共用同一个异步循环。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from .config import load_model_config
from .demo import DemoModel
from .errors import TurnLimitError
from .file_tools import read_tool
from .messages import AssistantMessage, Message, Text, ToolResultMessage, UserMessage
from .providers import ProviderError, create_model


async def run(options: argparse.Namespace, *, loop: Callable[..., Awaitable[list[Message]]]) -> int:
    """按依赖层次运行终端入口，真实客户端在 finally 中关闭。

    输入：已经解析的命令行参数；输出：进程退出码。
    流程：基础路径 → 工具 → 配置/模型 → 用户输入 → 公共循环 → 展示结果。
    边界：demo 不加载密钥；模型/工具正文仅在本入口打印，循环本身不打印。
    """
    workspace = options.workspace.resolve(strict=True)
    reader = read_tool(workspace)
    tools = [reader]
    if options.demo:
        model = DemoModel("read", {"path": "README.md"})
        max_tokens = 8000
    else:
        config = load_model_config(options.env_file)
        model = create_model(config)
        max_tokens = config.max_tokens
    history: list[Message] = []
    try:
        while True:
            if options.prompt is not None:
                prompt = options.prompt
            else:
                try:
                    prompt = input("you> ").strip()
                except EOFError:
                    return 0
                if prompt.lower() in {"", "q", "exit"}:
                    return 0
            start = len(history)
            history.append(UserMessage(prompt))
            await loop(
                history,
                model=model,
                tools=tools,
                max_tokens=max_tokens,
                max_turns=options.max_turns,
            )
            for message in history[start:]:
                if isinstance(message, ToolResultMessage):
                    status = "失败" if message.is_error else "结果"
                    print(f"[{message.tool_name} {status}] {message.content}")
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, Text):
                            print(block.text)
            last = history[-1]
            if isinstance(last, AssistantMessage) and last.usage is not None:
                print(f"[用量] 输入 {last.usage.input_tokens}，输出 {last.usage.output_tokens}")
            if isinstance(last, AssistantMessage) and last.stop_reason in {"error", "aborted"}:
                print(last.error_message or f"模型以 {last.stop_reason} 结束", file=sys.stderr)
                return 1
            if isinstance(last, AssistantMessage) and last.stop_reason == "length":
                print("模型回答被额度截断", file=sys.stderr)
                return 1
            if options.prompt is not None:
                return 0
    finally:
        if not options.demo:
            await model.aclose()


def main(
    arguments: Sequence[str] | None = None,
    *,
    loop: Callable[..., Awaitable[list[Message]]],
) -> int:
    """解析终端选项后运行异步应用，不执行安装或环境初始化。"""
    parser = argparse.ArgumentParser(description="Pi Code Agent：新的前三章异步实现")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--prompt", help="执行一条任务后退出")
    parser.add_argument("--demo", action="store_true", help="用离线脚本模型读取 README，无需密钥")
    parser.add_argument("--max-turns", type=int, help="可选模型轮数保护；省略时不限制")
    options = parser.parse_args(arguments)
    if options.max_turns is not None and options.max_turns < 1:
        parser.error("--max-turns 必须是正整数")
    try:
        return asyncio.run(run(options, loop=loop))
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except (OSError, ValueError, ProviderError, TurnLimitError) as error:
        print(str(error), file=sys.stderr)
        return 1
