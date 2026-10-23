"""命令入口负责配置和展示；Agent 负责运行时组装。"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from pi_code_agent.agent import create_agent
from pi_code_agent.config import load_environment, model_config_from_env, run_config_from_env
from pi_code_agent.events import TerminalEventSink
from pi_code_agent.models import create_model_provider
from pi_code_agent.registry import load_extensions


# ===== s17 修改：现有选项保持，配置来源和接线集中到明确边界 =====
# s17 修改：参考 Pi 的依赖注入，将配置解析放在所有客户端和会话创建之前。
def main(arguments: Sequence[str] | None = None) -> None:
    """读取配置快照并校验，再组装 SDK、注册能力和进入终端对话。

    输入：启动选项、启动目录 .env 和进程环境；输出：终端回答与持久化记录。
    流程：选项 → 配置快照 → 模型与运行配置 → Provider/Agent → 交互并关闭。
    边界：不修改 os.environ，不从目标项目加载宿主配置，不自动安装依赖。
    """
    # 来自 s16：保持；只解析原有五种选项，帮助与非法参数提前退出。
    parser = argparse.ArgumentParser(description="Pi Code Agent：Python 编码 Agent 与 Harness")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path, default=Path.cwd() / ".pi-code-agent")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--mode", choices=("read-only", "default"), default="read-only")
    parser.add_argument("--extension", action="append", default=[], metavar="MODULE")
    options = parser.parse_args(arguments)

    # s17 修改：文件位置固定、环境优先；先解析两组配置，失败不创建模型或会话。
    environment = load_environment(Path.cwd() / ".env", environ=os.environ)
    model_config = model_config_from_env(environment)
    run_config = run_config_from_env(environment, enable_compaction=True)

    # s17 修改：功能组件消费已校验配置，真实 HTTP 超时由模型客户端应用。
    provider = create_model_provider(config=model_config)
    terminal = TerminalEventSink()

    # s17 修改：回答额度与轮数接入 SDK，摘要策略继续沿用已有实现。
    agent = create_agent(
        provider=provider,
        workspace=options.workspace,
        data_dir=options.data_dir,
        session_path=options.session,
        mode=options.mode,
        compaction_policy=run_config.compaction_policy,
        summary_max_tokens=run_config.summary_max_tokens,
        timeout_seconds=model_config.timeout_seconds,
        max_tokens=run_config.max_tokens,
        max_rounds=run_config.max_rounds,
    )
    try:
        # 来自 s16：保持；可信扩展和终端仍由入口显式注册，不自动发现。
        load_extensions(agent.extensions, options.extension)
        agent.subscribe(terminal)
        print(
            f"工作区：{agent.workspace}\n会话：{agent.session.path}\n输入任务，/help 查看命令，q 退出。"
        )
        while True:
            try:
                prompt = input("code >> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in {"", "q", "exit"}:
                break
            # s18 新增：斜杠走命令入口，只有 handler 显式请求时才进入模型。
            if prompt.startswith("/"):
                agent.execute_command(prompt)
                continue
            agent.run(prompt)
            print(agent.session.session_stats().summary_text())
    finally:
        # s17 修改：配置接线后的注册失败也走已有关闭流程。
        agent.close()
