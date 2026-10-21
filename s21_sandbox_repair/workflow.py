"""s21：模型执行与 Harness 验证分工；重试有界，不以模型自述当作验收。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from s13_a_sandbox_copy.sandbox import SandboxError
from s16_repository_search.workspace import checked_cache_path
from s21_sandbox_repair.view import snapshot_fingerprint


# s21 新增：封装一次“保存任务→运行本章循环”，不是替代/导入别章 agent_loop。
@dataclass
class AgentRuntime:
    """将已组装的运行时提供给修复编排器。

    输入：本章 loop、请求器、会话、注册表、事件和 executor；输出：一次模型任务的消息。
    流程：保存用户输入 → 构建上下文 → 完整 loop → 工具分发后检查执行器状态。
    边界：不验证业务、不做重试；执行器故障不能被检索器的普通错误包装吞掉。
    """

    loop: Callable
    requester: Any
    session: Any
    extensions: Any
    emit: Callable
    executor: Any
    system: str
    max_rounds: int

    def run(self, prompt: str) -> list:
        """保存标准用户消息并运行一次 Agent，不承担外层修复重试。

        输入：任务上下文或上一轮真实测试反馈的 JSON 正文；输出：本次循环的消息。
        流程：检查执行器 → 持久化用户消息 → 投影会话 → 将组件显式传给本章 loop。
        错误向 RepairRunner 传播，不能把失败当作正常结束交给验证器。
        """
        self.executor.ensure_ready()
        self.session.append_message({"role": "user", "content": prompt})
        messages = self.session.build_context()
        return self.loop(
            messages,
            create_message=self.requester,
            dispatch=self.dispatch,
            system=self.system,
            hooks=self.extensions.hooks,
            tools=self.extensions.tools,
            emit=self.emit,
            save_message=self.session.append_message,
            save_assistant=self.session.append_assistant,
            save_tool_results=self.session.append_tool_results,
            max_rounds=self.max_rounds,
        )

    def dispatch(self, name: str, arguments: dict):
        """检索包装返回后再查致命状态；隔离失败一律停止，不让模型反复重试。"""
        self.executor.ensure_ready()
        result = self.extensions.dispatch(name, arguments)
        self.executor.ensure_ready()
        return result


class TestVerifier:
    """固定验证命令及其被验证版本，不接受模型提供的成功声明。

    输入：executor、用户选定 argv；输出：退出码、日志和源码指纹。
    流程：采集副本 → 容器验证 → 再采集 → 检查被测源码未变。
    边界：命令成功不等于需求正确；验证修改源码或后台进程变化时不能批准交付。
    """

    __test__ = False

    def __init__(self, *, executor, argv: list[str], baseline, test_root: str = "tests") -> None:
        """复制用户验证配置，模型工具不能修改此列表。"""
        if not argv or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv):
            raise ValueError("验证命令不能为空")
        self.executor = executor
        self.argv = list(argv)
        root = PurePosixPath(test_root)
        if (
            not test_root
            or root.is_absolute()
            or ".." in root.parts
            or root.as_posix() != test_root
            or "\\" in test_root
        ):
            raise ValueError("test_root 必须是项目内相对目录")
        self.test_root = test_root
        self.test_files = {
            path: data for path, data in baseline.files.items() if self._is_acceptance_file(path)
        }
        if not any(path.startswith(test_root + "/") for path in self.test_files):
            raise ValueError("测试目录没有可保护的验收文件；请先准备公开测试")

    def run(self) -> dict:
        """运行固定验收命令，并绑定它实际验证的业务版本。

        输入：构造时固定的 argv、公开测试基线及当前容器；输出：退出码、日志、指纹。
        流程：采集快照 → 核对测试未改 → 运行命令 → 再采集并核对版本一致。
        测试失败可供下一次修复；测试被削弱、验证时源码变化或协议错误则立即停止。
        """
        current = self.executor.capture_workspace()
        if {
            path: data for path, data in current.files.items() if self._is_acceptance_file(path)
        } != self.test_files:
            raise SandboxError("验收测试/配置已被修改；不能通过削弱测试验收")
        before = snapshot_fingerprint(current)
        result = self.executor.verify(list(self.argv))
        after = snapshot_fingerprint(self.executor.capture_workspace())
        if before != after:
            raise SandboxError("验证期间业务源码发生变化，不能把退出码用于当前版本")
        if result["exit_code"] == 0 and re.search(r"(?m)^Ran 0 tests\b", result["output"]):
            raise SandboxError("验证没有执行任何测试，不能验收")
        return {**result, "source_fingerprint": after, "argv": list(self.argv)}

    def _is_acceptance_file(self, path: str) -> bool:
        """固定公开测试及可影响收集/选择的配置，不由模型决定保护范围。"""
        return path.startswith(self.test_root + "/") or PurePosixPath(path).name in {
            "conftest.py",
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
        }


class RepairRunner:
    """执行一次有界的“修改→验证→失败反馈”闭环。

    输入：AgentRuntime、TestVerifier、WorkspaceSync、报告目录与尝试上限。
    输出：修复报告和未应用 Diff；只会在真实验证成功时标 tests_passed。
    流程：运行任务 → Harness 验证 → 保存报告/预览 → 成功结束或反馈后重试。
    边界：不自动回写/提交、不安装依赖；基础设施和模型错误不作为测试失败重试。
    """

    def __init__(
        self,
        *,
        agent: AgentRuntime,
        verifier: TestVerifier,
        sync,
        report_path: Path,
        max_attempts: int = 3,
    ) -> None:
        """保存独立职责组件；模型单次限轮和外层修复次数分别有界。"""
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("修复尝试上限必须为 1–5")
        self.agent, self.verifier, self.sync = agent, verifier, sync
        self.report_path = checked_cache_path(report_path)
        self.max_attempts = max_attempts

    def run(self, task_context) -> dict:
        """运行一项任务，逐次保存真实验证证据，失败不伪造最终成功。

        输入：s20 TaskContext；输出：tests_passed/exhausted/failed 报告。
        流程：取原始任务输入 → 运行 Agent → 固定测试 → 保存 Diff/报告
        → 若失败，追加有界验证反馈；达到次数则明确未完成。
        """
        prompt = task_context.message()["content"]
        self.sync.invalidate()
        report = {
            "status": "running",
            "attempts": [],
            "verification_claim": "配置命令成功不等于业务正确",
        }
        try:
            for attempt in range(1, self.max_attempts + 1):
                self.agent.run(prompt)
                result = self.verifier.run()
                patch = self.sync.preview()
                # s21 新增：预览版本也必须等于被验证版本，不能批准后台产生的新修改。
                if self.sync.preview_fingerprint != result["source_fingerprint"]:
                    raise SandboxError("验证后副本再次变化，需重新运行验证")
                report["attempts"].append(
                    {"attempt": attempt, **result, "diff_bytes": len(patch.encode())}
                )
                report["status"] = "tests_passed" if result["exit_code"] == 0 else "exhausted"
                self._save(report)
                if result["exit_code"] == 0:
                    self.sync.mark_verified(result["source_fingerprint"])
                    return report
                prompt = json.dumps(
                    {
                        "request": "根据验证失败修复当前副本，不修改验收命令；完成后停止。",
                        "verification_feedback": result,
                    },
                    ensure_ascii=False,
                )
            return report
        except (RuntimeError, ValueError, OSError, KeyboardInterrupt) as error:
            self.sync.invalidate()
            report.update(status="failed", error=str(error)[:1000])
            self._save(report)
            raise

    def _save(self, report):
        """保存固定宿主报告，不把验证日志交回模型当作可覆盖的配置。"""
        checked_cache_path(self.report_path)
        if self.report_path.exists() and (
            not self.report_path.is_file() or self.report_path.stat().st_nlink != 1
        ):
            raise ValueError("报告不是独立普通文件")
        with self.report_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, allow_nan=False, indent=2)
