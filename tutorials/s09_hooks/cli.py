# ===== s09 快照 =====
# 来自 s08：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s05：终端通过订阅逐段显示文本，支持完整响应模式。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .config import load_model_config
from .demo import DemoModel
from .errors import TurnLimitError
from .events import AgentEvent, EventListenerError
from .extensions import ExtensionRuntime, discover_extensions
from .file_tools import read_tool
from .messages import AssistantMessage, Message, Text, ToolResultMessage
from .providers import ProviderError, create_model
from .session import AgentSession, SessionStore


# ===== s05 修改 =====
# 来自 s05：保持；显示组件记录已输出的文本前缀，最终消息只补未显示部分，避免重复正文。
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
        model = DemoModel(
            options.demo_tool, {"path": "README.md"} if options.demo_tool == "read" else {}
        )
        max_tokens = 8000
    else:
        config = load_model_config(options.env_file)
        model = create_model(config)
        max_tokens = config.max_tokens
    runtime = ExtensionRuntime()
    # 来自 s06：保持；客户端先组装，存储和入口失败也由外层 finally 关闭拥有的客户端。
    try:
        home = Path(os.environ.get("PI_CODE_AGENT_HOME", str(Path.home() / ".pi-code-agent")))
        discovered = discover_extensions(
            workspace,
            global_dir=None if options.no_extensions else home / "extensions",
            paths=[path.resolve() for path in options.extension],
            project=options.trust_project_extensions and not options.no_extensions,
        )
        await runtime.load(discovered)
        for diagnostic in runtime.diagnostics:
            print(f"扩展加载失败 {diagnostic['source']}: {diagnostic['error']}", file=sys.stderr)
        if options.no_session:
            store = None
        elif options.session is not None:
            path = options.session
            store = (
                SessionStore.open(path, workspace)
                if path.exists()
                else SessionStore.create(path, workspace)
            )
        else:
            target_id = sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
            path = Path.cwd() / ".sessions" / "s09" / target_id / (uuid4().hex + ".jsonl")
            store = SessionStore.create(path, workspace)
        session = AgentSession(
            model=model,
            builtin_tools=tools,
            extensions=runtime,
            cwd=workspace,
            store=store,
            stream=not options.no_stream,
            max_tokens=max_tokens,
            max_turns=options.max_turns,
            loop=loop,
        )
        display = ConsoleDisplay()
        session.subscribe(display)
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
            # 来自 s06：保持；状态和持久化归会话所有，CLI 不再持有独立可变历史。
            # 来自 s03：保持；只统计本次输入新增的回答，旧会话用量不重复计入。
            start = len(session.messages)
            history = await session.prompt(prompt)
            last = history[-1] if history else None
            # 来自 s03：保持；只累计本次新增的最终回答，流式快照不会重复计入。
            input_tokens = output_tokens = requests = reported = 0
            for message in history[start:]:
                if isinstance(message, AssistantMessage):
                    requests += 1
                    if message.usage is not None:
                        reported += 1
                        input_tokens += message.usage.input_tokens
                        output_tokens += message.usage.output_tokens
            if reported:
                incomplete = "（部分请求未返回用量）" if reported < requests else ""
                print(f"[本次用量] 输入 {input_tokens}，输出 {output_tokens}{incomplete}")
            elif requests and not options.demo:
                print("[本次用量] 模型未返回用量")
            if isinstance(last, AssistantMessage) and last.stop_reason in {"error", "aborted"}:
                print(last.error_message or f"模型以 {last.stop_reason} 结束", file=sys.stderr)
                return 1
            if isinstance(last, AssistantMessage) and last.stop_reason == "length":
                print("模型回答被额度截断", file=sys.stderr)
                return 1
            if options.prompt is not None:
                return 0
    finally:
        runtime.close()
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
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--session", type=Path, help="打开已有会话或创建指定文件")
    storage.add_argument("--no-session", action="store_true", help="纯内存运行，不保存会话")
    parser.add_argument(
        "--extension",
        "-e",
        type=Path,
        action="append",
        default=[],
        help="加载明确选择的可信 Python 文件或目录",
    )
    parser.add_argument(
        "--no-extensions", action="store_true", help="关闭自动扩展发现，仍允许显式 --extension"
    )
    parser.add_argument(
        "--trust-project-extensions",
        action="store_true",
        help="允许加载工作区 .pi-code-agent/extensions",
    )
    parser.add_argument("--demo-tool", default="read", help="离线脚本模型请求的工具名称")
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
