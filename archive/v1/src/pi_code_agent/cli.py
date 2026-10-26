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
from pi_code_agent.registry import ExtensionAPI, load_extensions


# ===== s17 修改：现有选项保持，配置来源和接线集中到明确边界 =====
# s17 修改：参考 Pi 的依赖注入，将配置解析放在所有客户端和会话创建之前。
def main(arguments: Sequence[str] | None = None) -> None:
    """读取配置快照并校验，再组装 SDK、注册能力和进入终端对话。

    输入：启动选项、启动目录 .env 和进程环境；输出：终端回答与持久化记录。
    流程：定位扩展 → 声明参数 → 完整解析 → 配置校验 → 创建和绑定 → 初始化并交互。
    边界：不修改 os.environ，不从目标项目加载宿主配置，不自动安装依赖。
    """
    # s19 修改：保留原有五种选项，声明扩展后再完整解析，帮助无需创建运行时。
    parser = argparse.ArgumentParser(
        description="Pi Code Agent：Python 编码 Agent 与 Harness",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示启动帮助并退出")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path, default=Path.cwd() / ".pi-code-agent")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--mode", choices=("read-only", "default"), default="read-only")
    parser.add_argument("--extension", action="append", default=[], metavar="MODULE")
    # s19 新增：第一遍只定位扩展模块；不创建模型或 Session，不执行第二遍 register。
    options, _ = parser.parse_known_args(arguments)
    extensions = ExtensionAPI()
    load_extensions(extensions, options.extension)
    extensions.flags.add_to_parser(parser)
    options = parser.parse_args(arguments)
    if options.help:
        parser.print_help()
        raise SystemExit(0)
    extensions.flags.apply_namespace(options)

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
        extensions=extensions,
    )
    try:
        # 来自 s16：保持；可信扩展和终端仍由入口显式注册，不自动发现。
        # s19 修改：扩展已经声明，只订阅终端并启动一次，保证参数先于状态恢复生效。
        agent.subscribe(terminal)
        agent.initialize_extensions()
        # s18 修改：提示交互命令入口，与 argparse 的 --help 区分。
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
            # s18 新增：参考 lcc 的 REPL 先分发命令；改用共享 SDK 入口以保存历史、支持程序调用。
            if prompt.startswith("/"):
                agent.execute_command(prompt)
                continue
            agent.run(prompt)
            print(agent.session.session_stats().summary_text())
    finally:
        # s17 修改：配置接线后的注册失败也走已有关闭流程。
        agent.close()
