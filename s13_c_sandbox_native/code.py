#!/usr/bin/env python3
"""s13-C：执行沙箱 · 原生受限进程。

模型工具通过原生受限 Worker 直接修改项目；宿主负责模型、会话和变化报告。
启动显式授权写入后不逐条审批；隔离失败绝不回退到宿主。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s11_runtime_observability import code as runtime
from s12_skill_loading import code as previous
from s13_a_sandbox_copy.code import load_container_skills
from s13_a_sandbox_copy.sandbox import SandboxError
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from s13_a_sandbox_copy.workspace import WorkspaceLayout
from s13_c_sandbox_native.sandbox import NativeSandboxExecutor
from zero2pi.ui import format_error, format_user_prompt

# ===== 来自 s12：Provider、会话、压缩、事件与技能（保持） =====

Message = previous.Message
ModelResponse = previous.ModelResponse
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
AssistantSaver = previous.AssistantSaver
EventSink = previous.EventSink
AgentEvent = previous.AgentEvent
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextCompactor = previous.ContextCompactor
CompactedContextRequester = previous.CompactedContextRequester
BlockingModelRequester = previous.BlockingModelRequester
StreamingModelRequester = previous.StreamingModelRequester
EventDispatcher = previous.EventDispatcher
# 来自 s11：保持；观察层故障结束入口，由 finally 清理沙箱。
EventDispatchError = runtime.EventDispatchError
TerminalEventSink = previous.TerminalEventSink
JsonlTraceRecorder = previous.JsonlTraceRecorder
UsageTracker = previous.UsageTracker
SessionErrorRecorder = previous.SessionErrorRecorder
ContextSummarizer = previous.ContextSummarizer
create_model_provider = previous.create_model_provider
summary_request_options = previous.summary_request_options
show_summary_diagnostic = previous.show_summary_diagnostic
model_round_limit_from_env = previous.model_round_limit_from_env
execute_tool = previous.execute_tool
_get = previous._get
_tool_result_is_error = previous._tool_result_is_error
_positive_int_env = previous._positive_int_env

# ===== s13-C 修改：执行环境与启动授权 =====

# 来自 s13-A：保持；提示词只说明实际环境，不声称靠模型自律建立隔离。
# s13-C 修改：实际平台决定 shell，不向模型展示宿主隔离策略。
SYSTEM = (
    "你是当前真实项目中的编程 Agent，使用 Windows cmd.exe。"
    if os.name == "nt"
    else "你是当前真实项目中的编程 Agent，使用 /bin/sh。"
) + "只有任务需要操作工作区时才使用工具；优先使用专用工具，完成后停止。"
# 来自 s12：保持；工具名称与 schema 不变，只更正 bash 的真实运行环境。
TOOLS = deepcopy(previous.TOOLS)
# s13-C 修改：工具说明对应实际平台，不再使用 A 的固定 Linux 容器环境。
# s13-C 修改：按工具名更正环境，不依赖工具排列。
for tool in TOOLS:
    if tool["name"] == "bash":
        tool["description"] = (
            "在真实项目的原生受限进程执行 shell；Windows 使用 cmd.exe，其他平台使用 /bin/sh。"
        )
# 来自 s13-A：保持；配置与记录固定在 Harness，目标项目不再决定 .env 或记录位置。
HARNESS_ROOT = Path(__file__).resolve().parent.parent


# ===== 来自 s13-A：完整核心循环与失败收尾（保持） =====


def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., Any],
    dispatch: DispatchTool,
    system: str,
    hooks: Hooks,
    emit: EventSink,
    save_message: SessionSaver | None = None,
    save_assistant: AssistantSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
    # 来自 s12：保持；限轮仅在调用方明确配置时启用，不是默认完成策略。
    max_rounds: int | None = None,
) -> list[Message]:
    """运行带工具调用、会话保存和事件观察的 Agent 循环。

    输入：模型消息、请求器、工具分发器、原生环境的 system、Hooks 和保存回调。
    输出：本次运行的完整消息；没有工具调用或模型返回失败状态时结束，超限时报错。
    流程：请求模型 → 保存 assistant 与 usage → 执行工具 → 保存结果 → 再次请求。
    技能仍通过 read_file 加载；隔离失败时保存完整工具结果批次，然后停止。
    """
    # 来自 s12：保持；轮数只计算回答请求；摘要由请求包装器负责，不隐含在计数中。
    # 来自 s12：保持；默认不限制轮数，有明确配置时才停止。
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max_rounds 必须为正整数")
    # 来自 s12：保持；只有 None 回退默认工具，显式空列表不扩大能力。
    active_tools = TOOLS if tools is None else tools
    emit(AgentEvent(type="agent_start", data={"message_count": len(messages)}))

    # 来自 s12：保持；显式统计回答请求，None 时不设置硬停止点。
    round_count = 0
    while max_rounds is None or round_count < max_rounds:
        round_count += 1
        # 来自 s12：保持；摘要或回答请求失败同样结束本次任务，观察者不会收到悬空生命周期。
        try:
            response = create_message(
                system=system,
                messages=messages,
                tools=active_tools,
                max_tokens=max_tokens,
            )
        except EventDispatchError:
            # 来自 s11：保持；观察链失败后不再次广播结束事件。
            raise
        except (RuntimeError, ValueError):
            emit(
                AgentEvent(
                    type="agent_end",
                    data={"messages": list(messages), "reason": "request_failed"},
                )
            )
            raise
        assistant_message = {
            "role": "assistant",
            "content": s05._to_jsonable(response.content),
        }
        messages.append(assistant_message)
        if save_assistant is not None:
            save_assistant(assistant_message, response)
        elif save_message is not None:
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        if response.stop_reason in {"error", "aborted"}:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        tool_calls = [
            block for block in assistant_message["content"] if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        # 来自 s13-A：保持；致命隔离失败后不再执行剩余工具，但必须补齐结果配对。
        sandbox_failure: str | None = None
        for block in tool_calls:
            tool_call_id = str(_get(block, "id") or "")
            name = str(_get(block, "name") or "")
            arguments = _get(block, "input") or {}
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"tool_call_id": tool_call_id, "name": name, "arguments": arguments},
                )
            )

            tool_started_at = perf_counter()
            # 来自 s13-A：保持；工具只能走注入的 dispatch，后端失败不能继续重试。
            if sandbox_failure is not None:
                output = "Error: sandbox stopped; this tool was not executed"
            else:
                try:
                    output = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
                except (SandboxError, KeyboardInterrupt) as error:
                    sandbox_failure = str(error) or "用户中断隔离执行"
                    output = f"Error: {sandbox_failure}"
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000
            emit(
                AgentEvent(
                    type="tool_result",
                    data={
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "output": output,
                        "is_error": _tool_result_is_error(output),
                        "duration_ms": tool_duration_ms,
                    },
                )
            )
            results.append({"type": "tool_result", "tool_use_id": tool_call_id, "content": output})

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)
        # 来自 s13-A：保持；先保存整个批次，再终止；加载历史时不会留下缺失 tool_result。
        if sandbox_failure is not None:
            emit(
                AgentEvent(
                    type="agent_end", data={"messages": list(messages), "reason": "sandbox_failed"}
                )
            )
            raise SandboxError(sandbox_failure)

    # 来自 s12：保持；保留已完成工具结果并报告未完成，不伪造一次模型最终回答。
    emit(
        AgentEvent(
            type="agent_end",
            data={"messages": list(messages), "reason": "max_rounds"},
        )
    )
    raise RuntimeError(f"已达到 {max_rounds} 轮模型请求上限，任务尚未完成；已停止自动调用工具。")


# ===== s13-C 新增：显式授权后的真实工作区冒烟 =====


def smoke_test(executor: NativeSandboxExecutor) -> None:
    """验证直接写入、读取和 shell；专用文件保留，启动时必须授权真实写入。

    输入：就绪执行器；输出：真实项目中的唯一测试文件及验证结果。
    流程：随机名称写入 → read_file 检查 → Python shell 检查，不请求模型。
    不覆盖已有文件，不自动删除项目内容。
    """
    name = f"_sandbox_smoke_{uuid4().hex}.txt"
    result = executor.dispatch("write_file", {"path": name, "content": "sandbox-ok"})
    if result.startswith(("Error:", "Permission denied:")):
        raise SandboxError(result)
    if executor.dispatch("read_file", {"path": name}) != "sandbox-ok":
        raise SandboxError("冒烟读取失败")
    command = f"python -c \"from pathlib import Path; assert Path('{name}').read_text() == 'sandbox-ok'; print('ok')\""
    result = executor.dispatch("bash", {"command": command})
    if result.startswith(("Error:", "Permission denied:")):
        raise SandboxError(result)
    print(f"冒烟  已在真实工作区创建 {name}；{result}")


# ===== s13-C 修改：沿用请求链，删除副本交付组件 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """运行直接写入真实项目的原生沙箱 Agent，核心模型循环不封装。

    输入：工作区、--allow-write、runtime 目录、可选会话；输出：即时修改与记录。
    流程：授权 → 绑定工作区 → 启动后端 → 显式组装请求链 → 交互 → 收尾。
    边界：没有 /apply，退出不撤销已发生的修改，隔离失败不回退宿主。
    """
    parser = argparse.ArgumentParser(description="s13-C：原生沙箱直接操作真实项目")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="目标项目，默认当前目录")
    # s13-C 新增：授权必须来自本次 CLI，不能由模型或历史会话恢复。
    parser.add_argument("--allow-write", action="store_true", help="允许直接修改真实项目")
    parser.add_argument(
        "--runtime",
        type=Path,
        default=HARNESS_ROOT / ".sandbox-runtime/node_modules/@anthropic-ai/sandbox-runtime",
        help="已手动安装的固定版本 runtime 包目录",
    )
    parser.add_argument("--session", type=Path, help="本版本、本项目的历史会话")
    parser.add_argument("--smoke", action="store_true", help="真实写入冒烟，不调用模型")
    options = parser.parse_args(arguments)
    if not options.allow_write:
        parser.error("原生沙箱会直接修改文件；确认范围后传入 --allow-write")
    if options.smoke and options.session is not None:
        parser.error("--smoke 不能与 --session 同时使用")

    # 来自 s13-A：保持；布局按实际版本隔离记录，执行器只接收源码目录。
    workspace = WorkspaceLayout.for_path(options.workspace, HARNESS_ROOT, chapter="s13-c")
    source_root = workspace.source_root
    session_path = None
    if options.session is not None:
        session_path = workspace.select_session_path(options.session)
    snapshot = WorkspaceSnapshot.capture(source_root)
    # s13-C 修改：模型读真实路径；元数据仍从启动快照生成，不再声称路径位于 /work。
    container_catalog = previous.SkillCatalog(skills=())
    if not options.smoke:
        copied_catalog = load_container_skills(snapshot)
        native_skills = tuple(
            previous.Skill(
                skill.name, skill.description, source_root / skill.path.relative_to(Path("/work"))
            )
            for skill in copied_catalog.skills
        )
        container_catalog = previous.SkillCatalog(skills=native_skills)
    executor = NativeSandboxExecutor(
        workspace=source_root,
        harness_root=HARNESS_ROOT,
        runtime_root=options.runtime,
    )
    run_root = workspace.sandbox_root / executor.name
    # s13-C 新增：报告仍须留在 Harness，不能通过预置链接重定向。
    if run_root.resolve() != run_root or not run_root.is_relative_to(HARNESS_ROOT.resolve()):
        raise ValueError("运行报告目录被重定向或超出 Harness")
    diff_path = run_root / "changes.diff"
    # s13-C 新增：先建立将使用的记录目录，后端才能在启动时一并保护。
    if not options.smoke:
        workspace.session_root.mkdir(parents=True, exist_ok=True)
        workspace.trace_root.mkdir(parents=True, exist_ok=True)
    try:
        executor.start(run_root)
        snapshot.write_tar(run_root / "before.tar")
        print(f"s13-C：目标项目 {source_root}；真实文件直接生效；原生沙箱 {executor.name}")
        if options.smoke:
            smoke_test(executor)
            return

        # ===== 来自 s12：基础模型依赖与会话（保持） =====
        # 来自 s13-A：保持；明确加载 Harness 配置，不读取目标项目 .env，也不搜索其他目录。
        load_dotenv(dotenv_path=HARNESS_ROOT / ".env", override=True)
        timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
        max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
        provider = create_model_provider(timeout_seconds=timeout_seconds, max_retries=max_retries)
        # 来自 s13-A：保持；新会话保存目标绑定；恢复路径已在启动后端前校验。
        if session_path is None:
            session_path = workspace.select_session_path(None)
        session = SessionManager.open(session_path)

        # ===== 来自 s12：技能、观察层与模型层（保持） =====
        system_prompt = previous.build_system_prompt(SYSTEM, container_catalog)
        terminal = TerminalEventSink()
        # 来自 s13-A：保持；每次运行新建 Trace，但不同项目不再混在同一个章节目录。
        trace_path = runtime.new_trace_path(workspace.trace_root)
        trace_recorder = JsonlTraceRecorder(trace_path)
        usage_tracker = UsageTracker()
        error_recorder = SessionErrorRecorder(session)
        events = EventDispatcher()
        events.subscribe(terminal)
        events.subscribe(trace_recorder)
        events.subscribe(usage_tracker)
        events.subscribe(error_recorder)
        events.subscribe(show_summary_diagnostic)
        summary_requester = BlockingModelRequester(provider, timeout_seconds, events.emit)
        assistant_requester = StreamingModelRequester(provider, timeout_seconds, events.emit)

        # ===== 来自 s12：上下文组件（保持） =====
        policy = CompactionPolicy(
            context_window_tokens=_positive_int_env("MODEL_CONTEXT_WINDOW_TOKENS", 0),
            reserve_tokens=_positive_int_env("SESSION_COMPACTION_RESERVE_TOKENS", 16384),
            keep_recent_tokens=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000),
        )
        summary_default_tokens = int(policy.reserve_tokens * 0.8)
        summary_max_tokens = _positive_int_env(
            "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", summary_default_tokens
        )
        summary_options = summary_request_options(provider)
        summarizer = ContextSummarizer(
            create_message=summary_requester,
            max_tokens=summary_max_tokens,
            session=session,
            emit=events.emit,
            request_options=summary_options,
        )
        compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
        request_with_context = CompactedContextRequester(
            create_message=assistant_requester,
            session=session,
            compactor=compactor,
            emit=events.emit,
        )
        # 来自 s13-A：保持；边界由原生执行器提供，不注册宿主权限确认 Hook。
        hooks = Hooks()
        # 来自 s12：保持；轮数预算仍由显式配置控制，独立于执行隔离。
        round_limit = model_round_limit_from_env()
        print(f"模型：{provider.model}；可用技能：{len(container_catalog.skills)}")
        print(f"会话：{session.path}；Trace：{trace_path}")
        if options.session is not None:
            print("已恢复消息；本次写入授权不会从历史恢复。")
        # s13-C 修改：授权在启动时完成，交互中只有已发生变化的报告，不再确认补丁。
        print("工具直接修改真实项目；/diff 查看已发生的变化，q 退出；没有 /apply。\n")

        # ===== 来自 s13-A：交互与保存（保持）；s13-C 修改真实文件执行目标 =====
        while True:
            try:
                query = input(format_user_prompt("s13-C")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if query.lower() in {"", "q", "exit"}:
                return
            # s13-C 修改：报告已发生的变化，不再提供确认交付。
            if query == "/diff":
                current = WorkspaceSnapshot.capture(source_root)
                changes = snapshot.diff(current)
                diff_path.write_text(changes, encoding="utf-8")
                print(changes or "修改  本次启动后没有可报告的文本变化。")
                continue
            if query == "/apply":
                print("修改  原生工作区的修改已经生效，不需要 /apply。")
                continue
            user_message = {"role": "user", "content": query}
            session.append_message(user_message)
            active_context = session.build_context()
            try:
                agent_loop(
                    active_context,
                    create_message=request_with_context,
                    # 来自 s13-A：保持；所有六种工具都走同一原生受限 Worker，不能混入宿主文件 handler。
                    dispatch=executor.dispatch,
                    system=system_prompt,
                    hooks=hooks,
                    emit=events.emit,
                    tools=TOOLS,
                    save_message=session.append_message,
                    save_assistant=session.append_assistant,
                    max_rounds=round_limit,
                )
            except (SandboxError, EventDispatchError):
                # 来自 s11/s13-A：保持；观察层故障也必须结束入口并清理执行器。
                # 来自 s13-A：保持；隔离失败结束入口，由 finally 关闭执行器，不接受新任务。
                raise
            except (RuntimeError, ValueError) as error:
                print(format_error(str(error)), file=sys.stderr)
            finally:
                print(usage_tracker.summary_text())
                print(session.session_stats().summary_text())
            # s13-C 修改：只报告变化；外部编辑也可能出现在此报告中。
            current = WorkspaceSnapshot.capture(source_root)
            changes = snapshot.diff(current)
            diff_path.write_text(changes, encoding="utf-8")
            if changes:
                print("修改  工作区已有变化，立即生效；/diff 查看。")
            else:
                print("修改  本次启动后没有可报告的文本变化。")
    finally:
        # s13-C 修改：报告真实文件变化，不具有回滚或交付职责。
        original_error = sys.exc_info()[1]
        final_error = None
        try:
            if run_root.exists():
                current = WorkspaceSnapshot.capture(source_root)
                changes = snapshot.diff(current)
                diff_path.write_text(changes, encoding="utf-8")
                print(f"变化报告：{diff_path}；修改已经生效，退出不会撤销。")
        except (ValueError, OSError) as error:
            final_error = SandboxError(f"未保存变化报告：{error}")
            print(format_error(str(final_error)), file=sys.stderr)
        finally:
            try:
                executor.close()
                print("沙箱  执行环境已关闭；项目文件保留。")
            except (SandboxError, OSError) as error:
                final_error = SandboxError(f"执行环境清理失败：{error}")
                print(format_error(str(final_error)), file=sys.stderr)
        if final_error is not None and original_error is None:
            raise final_error


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, TypeError, OSError, KeyboardInterrupt) as error:
        print(format_error(str(error) or "用户中断"), file=sys.stderr)
        raise SystemExit(1) from error
