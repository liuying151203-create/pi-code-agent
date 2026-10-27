# ===== s05 修改 =====
# s05 修改：在本章内保存流式阶段的组件，不引用产品或其他教学版本。
"""s05：终端通过订阅逐段显示文本，支持完整响应模式。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from .config import load_model_config
from .demo import DemoModel
from .errors import TurnLimitError
from .events import AgentEvent, EventBus, EventListenerError
from .file_tools import read_tool
from .messages import AssistantMessage, Message, Text, ToolResultMessage, UserMessage
from .providers import ProviderError, create_model


# ===== s05 修改 =====
# s05 修改：显示组件记录已输出的文本前缀，最终消息只补未显示部分，避免重复正文。
class ConsoleDisplay:
    """订阅运行事件，把文本增量及时输出，保留最终消息的完整展示。

    输入：事件；输出：终端文字。流程：开始时重置 → 打印 delta → 结束时补后缀和换行。
    边界：只管理显示状态，不修改历史、不触发模型、不保存会话。
    """

    def __init__(self) -> None:
        self._text: dict[int, str] = {}

    def __call__(self, event: AgentEvent) -> None:
        """显示工具进度或模型文字，同一段流式正文只打印一次。"""
        if event["type"] == "tool_execution_start":
            print(f"[{event['tool_name']} 开始]", flush=True)
        elif event["type"] == "message_start" and isinstance(event["message"], AssistantMessage):
            self._text.clear()
        elif event["type"] == "message_update":
            change = event["assistant_message_event"]
            if change["type"] == "text_delta":
                index, delta = change["content_index"], change["delta"]
                print(delta, end="", flush=True)
                self._text[index] = self._text.get(index, "") + delta
        elif event["type"] == "message_end":
            message = event["message"]
            if isinstance(message, ToolResultMessage):
                status = "失败" if message.is_error else "结果"
                print(f"[{message.tool_name} {status}] {message.content}", flush=True)
            elif isinstance(message, AssistantMessage):
                has_text = False
                for index, block in enumerate(message.content):
                    if isinstance(block, Text):
                        has_text = True
                        shown = self._text.get(index, "")
                        remainder = block.text.removeprefix(shown)
                        print(remainder, end="", flush=True)
                if has_text or self._text:
                    print(flush=True)
                self._text.clear()


# ===== s04 修改 =====
# 来自 s04：保持；入口通过事件显示结果；s03 的无事件组装保存在 s03 自己的快照。
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
    # 来自 s04：保持；每个终端实例只有一份订阅表；不会在多轮输入时反复注册。
    events = EventBus()
    display = ConsoleDisplay()
    events.subscribe(display)
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
            # 来自 s04：保持；本次输入独立传入，旧历史不重播，消息只通过订阅显示。
            await loop(
                history,
                model=model,
                tools=tools,
                max_tokens=max_tokens,
                max_turns=options.max_turns,
                prompts=[UserMessage(prompt)],
                events=events,
                # s05 新增：命令行默认流式，兼容服务可显式退回完整响应。
                stream=not options.no_stream,
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
    loop: Callable[..., Awaitable[list[Message]]],
) -> int:
    """解析终端选项后运行异步应用，不执行安装或环境初始化。"""
    parser = argparse.ArgumentParser(description="Pi Code Agent：异步模型、工具与运行事件")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--prompt", help="执行一条任务后退出")
    parser.add_argument("--demo", action="store_true", help="用离线脚本模型读取 README，无需密钥")
    parser.add_argument("--max-turns", type=int, help="可选模型轮数保护；省略时不限制")
    parser.add_argument("--no-stream", action="store_true", help="使用完整响应，关闭流式输出")
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
