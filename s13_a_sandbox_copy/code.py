#!/usr/bin/env python3
"""s13-A：执行系统 · 最小 Docker Sandbox。

模型工具全部在无宿主挂载的 Linux 容器副本执行；宿主负责模型、会话和确认回写。
启动即明确选择 sandbox，不逐条审批；隔离失败绝不回退到宿主。
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

from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s11_runtime_observability import code as runtime
from s12_skill_loading import code as previous
from s13_a_sandbox_copy.sandbox import DEFAULT_IMAGE, SandboxError, SandboxExecutor
from s13_a_sandbox_copy.snapshot import WorkspaceSnapshot
from s13_a_sandbox_copy.workspace_sync import WorkspaceSync, WorkspaceSyncError
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

# ===== s13-A 修改：执行环境与启动边界 =====

# s13-A 修改：提示词只说明实际环境，不声称靠模型自律建立隔离。
SYSTEM = (
    "你是运行在 /work 项目副本中的编程 Agent，使用 Linux /bin/sh。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。"
)
# 来自 s12：保持；工具名称与 schema 不变，只更正 bash 的真实运行环境。
TOOLS = deepcopy(previous.TOOLS)
# s13-A 修改：不让 Windows cmd.exe 描述误导容器内的命令生成。
TOOLS[0]["description"] = "在容器 /work 执行 Linux /bin/sh 命令；最多返回末尾 2000 行 / 50 KiB。"
# s13-A 修改：独立保存会话、Trace 和审查报告，模型看不到这些宿主运行目录。
SESSION_ROOT = Path(".sessions/s13-a")
TRACE_ROOT = Path(".traces/s13-a")
SANDBOX_ROOT = Path(".sandboxes/s13-a")


# ===== 来自 s12：完整核心循环（保持）；s13-A 新增失败收尾 =====


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

    输入：模型消息、请求器、工具分发器、容器环境的 system、Hooks 和保存回调。
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
        # s13-A 新增：致命隔离失败后不再执行剩余工具，但必须补齐结果配对。
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
            # s13-A 修改：工具只走容器分发；失败不能被当作普通工具错误继续重试。
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
        # s13-A 新增：先保存整个批次，再终止；加载历史时不会留下缺失 tool_result。
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


# ===== s13-A 新增：无模型冒烟验证 =====


def smoke_test(executor: SandboxExecutor) -> None:
    """验证容器写入、读取、搜索和 shell，不加载密钥或请求模型。

    输入：已就绪执行器；输出：终端验证结果，修改只保留在 /work 副本。
    流程：写入专用测试文件 → 专用工具读取和定位 → shell 检查 uid 与内容。
    边界：命令由本章固定，不请求外部网络；真实资源隔离仍需 Docker 集成测试验证。
    """
    print(executor.dispatch("write_file", {"path": "_sandbox_smoke.txt", "content": "sandbox-ok"}))
    read_result = executor.dispatch("read_file", {"path": "_sandbox_smoke.txt"})
    if read_result != "sandbox-ok":
        raise SandboxError(f"冒烟读取失败：{read_result}")
    print(executor.dispatch("grep", {"path": "_sandbox_smoke.txt", "pattern": "sandbox-ok"}))
    command = (
        'python -c "import os; from pathlib import Path; '
        "assert os.getuid() == 65534; "
        "assert Path('_sandbox_smoke.txt').read_text() == 'sandbox-ok'; "
        "print('非 root shell 与项目副本可用')\""
    )
    result = executor.dispatch("bash", {"command": command})
    if result.startswith("Error:"):
        raise SandboxError(result)
    print(result)


# ===== s13-A 修改：显式组装隔离入口，不包装 main =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动在隔离副本中自动执行工具的 Agent。

    输入：明确的 sandbox 模式、本地镜像、可选历史会话及终端任务；--smoke 无需模型。
    输出：回答、用量、会话、Diff 和用户确认后的宿主修改。
    流程：准备副本 → 组装请求链与交付组件 → 任务后预览 → 确认回写 → 退出清理。
    边界：仅接受 sandbox；Docker 失败停止，不自动拉取、安装、联网或宿主回退。
    """
    # s13-A 修改：不扩展旧章节的模式；此入口只负责新增执行边界。
    parser = argparse.ArgumentParser(description="s13-A：Linux 容器副本中自动执行工具")
    parser.add_argument("--mode", choices=("sandbox",), default="sandbox")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="已手动构建的可信本地镜像")
    parser.add_argument("--session", type=Path, help="恢复消息历史，不恢复旧容器修改")
    parser.add_argument("--smoke", action="store_true", help="仅验证容器，不调用模型")
    options = parser.parse_args(arguments)

    # ===== s13-A 新增：基础依赖与执行组件 =====
    source_root = Path.cwd().resolve()
    snapshot = WorkspaceSnapshot.capture(source_root)
    executor = SandboxExecutor(image=options.image)
    run_root = SANDBOX_ROOT / executor.name
    # s13-A 新增：宿主报告目录也不能通过预置链接逃出当前项目。
    if not run_root.resolve().is_relative_to(source_root):
        raise ValueError("Sandbox 报告目录超出当前项目")
    diff_path = run_root / "changes.diff"
    # s13-A 新增：交付组件只归宿主入口持有，模型仍只能调用容器内六种工具。
    workspace_sync = WorkspaceSync(
        source_root=source_root,
        executor=executor,
        baseline=snapshot,
        report_path=diff_path,
    )
    try:
        # s13-A 新增：先验证 Docker，再初始化模型；副本不包含宿主会话与常见凭证。
        executor.start(snapshot, run_root)
        print("s13-A：执行沙箱 · 隔离副本")
        print(f"容器：{executor.name}；工作区：/work；网络：禁用；工具：自动执行")
        print(f"输入快照：{len(snapshot.files)} 个普通文件；Diff：{diff_path}")
        if options.smoke:
            smoke_test(executor)
            return

        # ===== 来自 s12：基础模型依赖与会话（保持） =====
        load_dotenv(override=True)
        timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
        max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
        provider = create_model_provider(timeout_seconds=timeout_seconds, max_retries=max_retries)
        session_path = s05.select_session_path(options.session, session_root=SESSION_ROOT)
        session = SessionManager.open(session_path)

        # ===== 来自 s12：技能、观察层与模型层（保持） =====
        skill_catalog = previous.SkillCatalog.scan(previous.SKILLS_ROOT)
        # s13-A 修改：只提供已复制的技能，路径改为容器路径，不暴露宿主绝对路径。
        copied_skills = tuple(
            previous.Skill(
                skill.name, skill.description, Path("/work") / skill.path.relative_to(source_root)
            )
            for skill in skill_catalog.skills
            if skill.path.relative_to(source_root).as_posix() in snapshot.files
        )
        container_catalog = previous.SkillCatalog(skills=copied_skills)
        system_prompt = previous.build_system_prompt(SYSTEM, container_catalog)
        terminal = TerminalEventSink()
        trace_path = runtime.new_trace_path(TRACE_ROOT)
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
        # s13-A 修改：边界由容器执行器提供，不注册宿主权限确认 Hook。
        hooks = Hooks()
        # 来自 s12：保持；轮数预算仍由显式配置控制，不等同于容器隔离。
        round_limit = model_round_limit_from_env()
        print(f"模型：{provider.model}；可用技能：{len(copied_skills)}")
        print(f"会话：{session.path}；Trace：{trace_path}")
        if options.session is not None:
            print("已恢复消息；执行工作区仍是原仓库的新副本，不恢复上次容器修改。")
        # s13-A 新增：明确回写需要单独确认，完成任务不再依赖退出才生成报告。
        print("输入任务；/diff 查看修改，/apply 确认应用，q 退出。未确认不会写回原仓库。\n")

        # ===== 来自 s12：交互与保存（保持）；s13-A 修改工具分发目标 =====
        while True:
            try:
                query = input(format_user_prompt("s13-A")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if query.lower() in {"", "q", "exit"}:
                return
            # s13-A 新增：宿主命令在保存消息之前处理，不进入模型上下文和请求统计。
            if query == "/diff":
                print(workspace_sync.preview() or "修改  没有待应用修改。")
                continue
            if query == "/apply":
                if not workspace_sync.changed_files:
                    print("修改  没有已预览的待应用修改；可用 /diff 刷新。")
                    continue
                print(f"修改  将应用 {len(workspace_sync.changed_files)} 个文件：")
                for name in workspace_sync.changed_files:
                    print(f"      {name!r}")
                try:
                    answer = input("是否应用到原仓库？[y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n修改  已取消确认，原仓库未被回写。")
                    continue
                if answer != "y":
                    print("修改  已取消，原仓库未被回写。")
                    continue
                # s13-A 新增：确认和实际写入分开处理，中断写入不能声称宿主从未变化。
                try:
                    applied_count = workspace_sync.apply()
                    print(f"修改  已应用 {applied_count} 个文件；容器继续保留。")
                except (WorkspaceSyncError, OSError) as error:
                    print(format_error(str(error)), file=sys.stderr)
                except KeyboardInterrupt:
                    print("\n修改  回写中断，请检查 apply 补丁、before 备份和宿主文件。")
                continue
            user_message = {"role": "user", "content": query}
            session.append_message(user_message)
            active_context = session.build_context()
            try:
                agent_loop(
                    active_context,
                    create_message=request_with_context,
                    # s13-A 修改：所有六种工具都走同一容器，不能混入宿主文件 handler。
                    dispatch=executor.dispatch,
                    system=system_prompt,
                    hooks=hooks,
                    emit=events.emit,
                    tools=TOOLS,
                    save_message=session.append_message,
                    save_assistant=session.append_assistant,
                    max_rounds=round_limit,
                )
            except SandboxError:
                # s13-A 新增：隔离失败结束整个入口，由 finally 关闭容器，不再接受新任务。
                raise
            except (RuntimeError, ValueError) as error:
                print(format_error(str(error)), file=sys.stderr)
            finally:
                print(usage_tracker.summary_text())
                print(session.session_stats().summary_text())
            # s13-A 新增：正常完成或普通模型错误后都可审查当前副本；致命隔离失败不走这里。
            workspace_sync.preview()
            if workspace_sync.changed_files:
                print(
                    f"修改  {len(workspace_sync.changed_files)} 个文件待应用；/diff 查看，/apply 确认。"
                )
            else:
                print("修改  没有待应用修改。")
    finally:
        # s13-A 新增：只导出报告，不解压不可信文件、不写回原仓库；失败时明确说明。
        original_error = sys.exc_info()[1]
        final_error: SandboxError | None = None
        try:
            if run_root.exists():
                # s13-A 修改：退出只保存相对最近交付基线的未应用修改，不重复导出已交付内容。
                workspace_sync.preview()
                # s13-A 修改：按实际剩余变化提示，空 Diff 不再误导为仍有修改未交付。
                if workspace_sync.changed_files:
                    print(f"未应用 Diff 已保存：{diff_path}（退出不会自动回写）")
                else:
                    print("修改  没有未应用修改。")
        except (SandboxError, ValueError, OSError) as error:
            final_error = SandboxError(f"未导出最终修改：{error}")
            print(format_error(str(final_error)), file=sys.stderr)
        finally:
            try:
                executor.close()
                # s13-A 修改：清理成功后才报告完成；启动前失败不声称清理了容器。
                if run_root.exists():
                    print("沙箱  容器已清理。")
            except SandboxError as error:
                final_error = SandboxError(f"容器清理失败；需手动清理 {executor.name}：{error}")
                print(
                    format_error(f"容器清理失败：{error}；需手动清理 {executor.name}"),
                    file=sys.stderr,
                )
        # s13-A 新增：正常结束时导出/清理失败也返回失败；已有异常保留原始原因。
        if final_error is not None and original_error is None:
            raise final_error


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyboardInterrupt) as error:
        print(format_error(str(error) or "用户中断"), file=sys.stderr)
        raise SystemExit(1) from error
