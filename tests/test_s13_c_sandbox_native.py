"""s13-C 协议与策略测试；假的启动器不证明真实 OS 隔离。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from s13_a_sandbox_copy.sandbox import SandboxError
from s13_c_sandbox_native import sandbox, tool_worker

HARNESS = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def normal_user_for_offline_backend(request, monkeypatch):
    """离线测试模拟普通用户；真实隔离测试必须使用实际非 root 用户。"""
    if "real_native" not in request.node.name and hasattr(os, "geteuid"):
        monkeypatch.setattr(sandbox.os, "geteuid", lambda: 1000)
    if "real_native" not in request.node.name:
        monkeypatch.setattr(
            sandbox.shutil, "which", lambda name: str(Path(sys.executable).parent / "node")
        )


class NativeStub:
    """只模拟固定 Worker 返回，不执行任何模型命令。"""

    def __init__(self, project):
        self.project = project
        self.calls = []
        self.failure = False

    def __call__(self, arguments, *, data, cwd, timeout):
        self.calls.append((arguments, data, cwd))
        if self.failure:
            return b'{"fatal":"broken isolation"}'
        request = json.loads(data)
        if request["action"] == "tool" and request["name"] == "write_file":
            args = request["arguments"]
            (self.project / args["path"]).write_text(args["content"], encoding="utf-8")
        return b'{"output":"ready"}' if request["action"] == "probe" else b'{"output":"ok"}'


def make_native(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "dist").mkdir(parents=True)
    (runtime / "dist/cli.js").write_text("trusted fixture", encoding="utf-8")
    (runtime / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/sandbox-runtime", "version": sandbox.RUNTIME_VERSION}),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("target-secret", encoding="utf-8")
    stub = NativeStub(project)
    executor = sandbox.NativeSandboxExecutor(project, HARNESS, runtime, runner=stub)
    return executor, stub


def test_native_settings_and_fixed_worker_do_not_delegate_host_handlers(tmp_path):
    executor, stub = make_native(tmp_path)
    report = tmp_path / "report"
    try:
        executor.start(report)
        settings = json.loads((report / "settings.json").read_text(encoding="utf-8"))
        assert settings["network"]["allowedDomains"] == []
        fs = settings["filesystem"]
        assert {"path": str(executor.workspace), "literal": True} in fs["allowWrite"]
        assert {"path": str(executor.workspace / ".env"), "literal": True} in fs["denyRead"]
        assert {"path": str(report), "literal": True} in fs["denyWrite"]
        assert (report / "trusted/base_tools.py").read_bytes() == (
            HARNESS / "s02_tool_use/code.py"
        ).read_bytes()
        argv = stub.calls[0][0]
        assert "--settings" in argv and "-c" in argv
        assert "tool_worker.py" in argv[-1] and "-I" in argv[-1]
        if os.name == "nt":
            assert argv[-1].startswith('""') and argv[-1].endswith('""')
        command = "echo never reaches host shell & something"
        executor.dispatch("bash", {"command": command})
        argv, data, cwd = stub.calls[-1]
        assert command not in " ".join(argv)
        assert json.loads(data)["arguments"]["command"] == command
        assert cwd == executor.workspace
        executor.dispatch("write_file", {"path": "new.py", "content": "x = 1"})
        assert (executor.workspace / "new.py").read_text() == "x = 1"
    finally:
        temporary = executor._temporary.name if executor._temporary else None
        executor.close()
        if temporary:
            assert not Path(temporary).exists()


@pytest.mark.parametrize("version", ["0.0.26", "0.0.80"])
def test_wrong_runtime_version_fails_before_worker_or_model(tmp_path, version):
    executor, stub = make_native(tmp_path)
    (executor.runtime_root / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/sandbox-runtime", "version": version}), encoding="utf-8"
    )
    with pytest.raises(SandboxError, match="手动准备"):
        executor.start(tmp_path / "report")
    assert stub.calls == []
    assert not (tmp_path / "report").exists()


def test_failed_probe_and_tool_call_cannot_fall_back_to_local_execution(tmp_path):
    executor, stub = make_native(tmp_path)
    stub.failure = True
    try:
        with pytest.raises(SandboxError, match="broken isolation"):
            executor.start(tmp_path / "report")
        with pytest.raises(SandboxError, match="未就绪"):
            executor.dispatch("write_file", {"path": "should-not-exist", "content": "x"})
        assert not (executor.workspace / "should-not-exist").exists()
    finally:
        executor.close()


def test_native_fatal_after_start_disables_further_calls(tmp_path):
    executor, stub = make_native(tmp_path)
    try:
        executor.start(tmp_path / "report")
        stub.failure = True
        with pytest.raises(SandboxError):
            executor.dispatch("bash", {"command": "anything"})
        calls = len(stub.calls)
        with pytest.raises(SandboxError, match="未就绪"):
            executor.dispatch("bash", {"command": "anything"})
        assert len(stub.calls) == calls
    finally:
        executor.close()


def test_probe_detects_an_unconfined_read_before_any_model_command(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"native-probe-secret")
    response = tool_worker.probe({"secret": str(secret), "forbidden_write": "unused", "port": 1})
    assert "未阻止" in response["fatal"]


def test_native_runner_scrubs_model_credentials_and_node_options(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "must-not-leak")
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    code = "import os,json; print(json.dumps({k:os.getenv(k) for k in ['MODEL_API_KEY','NODE_OPTIONS']}))"
    output = sandbox.run_native([sys.executable, "-c", code], data=b"", cwd=tmp_path)
    assert json.loads(output) == {"MODEL_API_KEY": None, "NODE_OPTIONS": None}


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows cmd 的原始命令行引号")
def test_fixed_windows_worker_command_handles_spaces_without_running_runtime(tmp_path):
    """只执行可信 Worker 的临时文件写入来验证 cmd 字节；不冒充原生隔离测试。"""
    executor, _ = make_native(tmp_path)
    try:
        executor.start(tmp_path / "report with spaces")
        # 上游 Windows 内层 cmd 使用 raw passthrough；不要再用 list2cmdline 重转引号。
        command = os.environ["COMSPEC"] + " /d /s /c " + executor._arguments[-1]
        result = subprocess.run(
            command,
            cwd=executor.workspace,
            shell=False,
            check=False,
            capture_output=True,
            input=json.dumps(
                {
                    "action": "tool",
                    "name": "write_file",
                    "arguments": {"path": "quoting.py", "content": "x = 1\n"},
                }
            ).encode("utf-8"),
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert "fatal" not in json.loads(result.stdout)
        assert (executor.workspace / "quoting.py").read_text() == "x = 1\n"
    finally:
        executor.close()


def test_native_runner_reports_timeout_and_output_limit(tmp_path, monkeypatch):
    with pytest.raises(SandboxError, match="超过"):
        sandbox.run_native(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            data=b"",
            cwd=tmp_path,
            timeout=0.1,
        )
    monkeypatch.setattr(sandbox, "MAX_OUTPUT", 32)
    with pytest.raises(SandboxError, match="输出超限"):
        sandbox.run_native([sys.executable, "-c", "print('x' * 1000)"], data=b"", cwd=tmp_path)


@pytest.mark.skipif(
    os.getenv("SANDBOX_C_INTEGRATION") != "1", reason="显式开启原生 runtime 集成测试"
)
def test_real_native_runtime_denies_secrets_outside_write_and_network(tmp_path):
    runtime = Path(os.environ["SANDBOX_RUNTIME_ROOT"]).resolve()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("host-secret", encoding="utf-8")
    executor = sandbox.NativeSandboxExecutor(project, HARNESS, runtime)
    try:
        executor.start(tmp_path / "report")
        executor.dispatch("write_file", {"path": "real.py", "content": "x = 1"})
        assert (project / "real.py").read_text() == "x = 1"
        assert executor.dispatch("read_file", {"path": ".env"}).startswith("Permission denied")
        # 启动 probe 已实测网络、保护读取、报告写入；继续实测 shell 不能绕过文件工具。
        command = "python -c \"from pathlib import Path; Path('.env').write_text('changed')\""
        assert executor.dispatch("bash", {"command": command}).startswith("Error:")
        assert (project / ".env").read_text() == "host-secret"
    finally:
        executor.close()
