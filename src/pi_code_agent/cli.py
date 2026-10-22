"""命令入口负责配置和展示；Agent 负责运行时组装。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from pi_code_agent.agent import create_agent
from pi_code_agent.compaction_base import CompactionPolicy, _positive_int_env
from pi_code_agent.events import TerminalEventSink
from pi_code_agent.models import create_model_provider
from pi_code_agent.registry import load_extensions


def main(arguments=None) -> None:
    """解析配置 → 组装 SDK → 注册可信能力 → 对话；不自动安装或构建。

    输入：workspace、data-dir、session、mode 和可信 extension 模块。
    输出：终端对话与持久化记录；错误向调用者传播，不伪装任务成功。
    """
    parser = argparse.ArgumentParser(description="Pi Code Agent：Python 编码 Agent 与 Harness")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path, default=Path.cwd() / ".pi-code-agent")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--mode", choices=("read-only", "default"), default="read-only")
    parser.add_argument("--extension", action="append", default=[], metavar="MODULE")
    options = parser.parse_args(arguments)

    load_dotenv()
    timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    provider = create_model_provider(
        timeout_seconds=timeout, max_retries=int(os.getenv("MODEL_MAX_RETRIES", "0"))
    )
    policy = CompactionPolicy(
        context_window_tokens=_positive_int_env("MODEL_CONTEXT_WINDOW_TOKENS", 0),
        reserve_tokens=_positive_int_env("SESSION_COMPACTION_RESERVE_TOKENS", 16384),
        keep_recent_tokens=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000),
    )
    agent = create_agent(
        provider=provider,
        workspace=options.workspace,
        data_dir=options.data_dir,
        session_path=options.session,
        mode=options.mode,
        compaction_policy=policy,
        summary_max_tokens=_positive_int_env(
            "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", int(policy.reserve_tokens * 0.8)
        ),
        timeout_seconds=timeout,
    )
    load_extensions(agent.extensions, options.extension)
    agent.subscribe(TerminalEventSink())
    print(f"工作区：{agent.workspace}\n会话：{agent.session.path}\n输入任务，q 退出。")
    try:
        while True:
            try:
                prompt = input("code >> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in {"", "q", "exit"}:
                break
            agent.run(prompt)
            print(agent.session.session_stats().summary_text())
    finally:
        agent.close()
