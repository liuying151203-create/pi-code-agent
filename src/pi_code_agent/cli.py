"""s04：终端通过订阅展示当前进度，使用本章的模型和工具组件。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from pi_code_agent.config import load_model_config
from pi_code_agent.demo import DemoModel
from pi_code_agent.events import AgentEvent, EventBus, EventListenerError
from pi_code_agent.file_tools import read_tool
from pi_code_agent.loop import TurnLimitError, agent_loop
from pi_code_agent.messages import AssistantMessage, Message, Text, ToolResultMessage, UserMessage
from pi_code_agent.providers import ProviderError, create_model


# ===== s04 新增 =====
# s04 新增：终端只观察事件，不往模型历史写显示文本。
def display_event(event: AgentEvent) -> None:
    """将工具开始和已完成消息显示到终端，不触发模型请求或持久化。

    输入：运行事件；输出：终端文本。流程：按事件类型取出实际结果并显示。
    边界：当前收到完整消息才显示文本，逐字输出留到 s05。
    """
    if event["type"] == "tool_execution_start":
        print(f"[{event['tool_name']} 开始]", flush=True)
    elif event["type"] == "message_end":
        message = event["message"]
        if isinstance(message, ToolResultMessage):
            status = "失败" if message.is_error else "结果"
            print(f"[{message.tool_name} {status}] {message.content}", flush=True)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, Text):
                    print(block.text, flush=True)


# ===== s04 修改 =====
# s04 修改：入口通过事件显示结果；s03 的无事件组装保存在 s03 自己的快照。
async def run(
    options: argparse.Namespace,
    *,
    loop: Callable[..., Awaitable[list[Message]]],
) -> int:
    """按依赖层次运行终端入口，真实客户端在 finally 中关闭。

    输入：已经解析的命令行参数；输出：进程退出码。
    流程：基础路径 → 工具 → 配置/模型 → 订阅 → 新输入 → 公共循环。
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
    # s04 新增：每个终端实例只有一份订阅表；不会在多轮输入时反复注册。
    events = EventBus()
    events.subscribe(display_event)
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
            # s04 修改：本次输入独立传入，旧历史不重播，消息只通过订阅显示。
            await loop(
                history,
                model=model,
                tools=tools,
                max_tokens=max_tokens,
                max_turns=options.max_turns,
                prompts=[UserMessage(prompt)],
                events=events,
            )
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
    loop: Callable[..., Awaitable[list[Message]]] = agent_loop,
) -> int:
    """解析终端选项后运行异步应用，不执行安装或环境初始化。"""
    parser = argparse.ArgumentParser(description="Pi Code Agent：异步模型、工具与运行事件")
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
    except (OSError, ValueError, ProviderError, TurnLimitError, EventListenerError) as error:
        print(str(error), file=sys.stderr)
        return 1
