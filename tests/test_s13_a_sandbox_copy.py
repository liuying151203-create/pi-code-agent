"""s13-A 离线验证边界与生命周期；不自动构建镜像或启动 Docker。"""

import base64
import io
import json
import os
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from s02_tool_use import code as base_tools
from s10_model_provider.code import ModelResponse, ModelUsage
from s13_a_sandbox_copy import code as chapter
from s13_a_sandbox_copy import sandbox, snapshot, tool_worker
from s13_a_sandbox_copy.workspace_sync import WorkspaceSync


class DockerStub:
    """只模拟 Docker 协议出口，保留真实快照、执行器、入口和 Agent loop。"""

    def __init__(self):
        self.calls = []
        self.files = {}
        self.fatal = False

    def __call__(self, arguments, *, data=None, timeout=30):
        self.calls.append((arguments, data, timeout))
        if arguments[1:3] == ["image", "inspect"]:
            return ("sha256:" + "a" * 64 + " linux").encode()
        if arguments[1] == "exec" and arguments[-1] == "--init":
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                assert all(member.isfile() for member in archive.getmembers())
                self.files = {member.name: archive.extractfile(member).read() for member in archive}
            return b'{"output":"ready"}'
        if arguments[1] == "exec":
            request = json.loads(data)
            if request["action"] == "snapshot":
                payload = snapshot.WorkspaceSnapshot(self.files).to_payload()
                return json.dumps({"files": payload}).encode()
            if self.fatal:
                return b'{"fatal":"worker failed"}'
            name, args = request["name"], request["arguments"]
            if name == "write_file":
                self.files[args["path"]] = args["content"].encode()
                output = "Wrote copy"
            elif name == "read_file":
                output = self.files[args["path"]].decode()
            else:
                output = "container tool result"
            return json.dumps({"output": output}).encode()
        return b"container-id"


def test_snapshot_filters_secrets_dependencies_and_runtime_data(tmp_path):
    paths = [
        "README.md",
        ".env",
        ".ENV.local",
        ".env.example",
        ".aws/credentials",
        ".ssh/id_rsa",
        ".git/config",
        ".sessions/history.jsonl",
        ".sandboxes/old/source.tar",
        "a.pem",
        ".venv/a.py",
        "node_modules/a.js",
        "src/app.py",
    ]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    captured = snapshot.WorkspaceSnapshot.capture(tmp_path)
    assert set(captured.files) == {"README.md", ".env.example", "src/app.py"}


def test_snapshot_does_not_follow_directory_or_file_links(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (workspace / "linked.txt").symlink_to(outside)
        (workspace / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 不允许创建符号链接")
    assert snapshot.WorkspaceSnapshot.capture(workspace).files == {}


def test_snapshot_limits_before_upload(tmp_path, monkeypatch):
    (tmp_path / "large.txt").write_bytes(b"12345")
    monkeypatch.setattr(snapshot, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="快照超过"):
        snapshot.WorkspaceSnapshot.capture(tmp_path)


def test_snapshot_does_not_copy_hard_links(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not for the model", encoding="utf-8")
    try:
        os.link(outside, workspace / "alias.txt")
    except OSError:
        pytest.skip("文件系统不支持硬链接")
    assert snapshot.WorkspaceSnapshot.capture(workspace).files == {}


@pytest.mark.parametrize(
    "name",
    ["../outside", "/absolute", "C:/host", "a/../b", "a\\b", ".env", ".git/config", ".", "a\nb"],
)
def test_export_paths_are_untrusted_and_not_written_to_host(name):
    with pytest.raises(ValueError):
        snapshot.WorkspaceSnapshot.from_payload({name: ""})


def test_export_payload_limits_and_type_validation(monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_BYTES", 2)
    with pytest.raises(ValueError, match="超过"):
        snapshot.WorkspaceSnapshot.from_payload({"a.txt": base64.b64encode(b"123").decode()})
    with pytest.raises(TypeError):
        snapshot.WorkspaceSnapshot.from_payload({"a.txt": 123})
    with pytest.raises(ValueError):
        snapshot.WorkspaceSnapshot.from_payload({"a.txt": "not-base64"})


def test_diff_covers_added_modified_deleted_binary_and_empty_files():
    before = snapshot.WorkspaceSnapshot(
        {"a.txt": b"old", "deleted.txt": b"gone\n", "a.bin": b"\xff"}
    )
    after = snapshot.WorkspaceSnapshot(
        {"a.txt": b"new", "added.txt": b"added\n", "empty": b"", "a.bin": b"\xfe"}
    )
    diff = before.diff(after)
    assert "-old\n\\ No newline at end of file" in diff
    assert "+new" in diff and "+added" in diff and "-gone" in diff
    assert "Binary file changed" in diff and "Empty file added/deleted" in diff


def test_executor_fixes_security_boundary_and_stdin_not_host_shell(tmp_path):
    docker = DockerStub()
    executor = sandbox.SandboxExecutor(runner=docker)
    baseline = snapshot.WorkspaceSnapshot({"README.md": b"original"})
    executor.start(baseline, tmp_path / "run")
    command = "echo x && echo '$(anything)' > local.txt"
    assert executor.dispatch("bash", {"command": command}) == "container tool result"
    startup = next(args for args, _, _ in docker.calls if args[1] == "run")
    for key, value in {
        "--network": "none",
        "--pull": "never",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges:true",
        "--user": "65534:65534",
        "--memory": "1g",
        "--memory-swap": "1g",
        "--cpus": "1",
        "--pids-limit": "64",
    }.items():
        assert startup[startup.index(key) + 1] == value
    assert "--read-only" in startup and "--init" in startup
    assert not {
        "--mount",
        "--volume",
        "--privileged",
        "--env-file",
        "--publish",
        "--use-api-socket",
    } & set(startup)
    assert "sha256:" + "a" * 64 in startup
    tool_args, tool_stdin, _ = docker.calls[-1]
    assert command not in tool_args
    assert json.loads(tool_stdin)["arguments"]["command"] == command
    assert tool_args[-3:] == ["python", "-I", sandbox.WORKER]
    executor.dispatch("write_file", {"path": "README.md", "content": "modified"})
    delivery = WorkspaceSync(tmp_path, executor, baseline, tmp_path / "changes.diff")
    delivery.preview()
    assert "-original" in (tmp_path / "changes.diff").read_text(encoding="utf-8")
    executor.close()
    assert docker.calls[-1][0] == ["docker", "rm", "--force", executor.name]


def test_worker_failure_invalidates_executor_and_never_falls_back(tmp_path):
    docker = DockerStub()
    executor = sandbox.SandboxExecutor(runner=docker)
    executor.start(snapshot.WorkspaceSnapshot({}), tmp_path / "run")
    docker.fatal = True
    with pytest.raises(sandbox.SandboxError, match="worker failed"):
        executor.dispatch("bash", {"command": "anything"})
    count = len(docker.calls)
    with pytest.raises(sandbox.SandboxError, match="未就绪"):
        executor.dispatch("bash", {"command": "anything else"})
    assert len(docker.calls) == count
    executor.close()


def test_missing_image_does_not_pull_create_or_dispatch(tmp_path):
    calls = []

    def unavailable(arguments, **kwargs):
        calls.append(arguments)
        raise sandbox.SandboxError("image missing")

    executor = sandbox.SandboxExecutor(runner=unavailable)
    with pytest.raises(sandbox.SandboxError):
        executor.start(snapshot.WorkspaceSnapshot({}), tmp_path / "run")
    executor.close()
    assert len(calls) == 1 and calls[0][1:3] == ["image", "inspect"]
    assert not (tmp_path / "run").exists()


def test_failed_start_can_only_cleanup_its_unique_container(tmp_path):
    docker = DockerStub()

    def fail_copy(arguments, **kwargs):
        if arguments[1] == "exec" and arguments[-1] == "--init":
            raise sandbox.SandboxError("copy failed")
        return docker(arguments, **kwargs)

    executor = sandbox.SandboxExecutor(runner=fail_copy)
    with pytest.raises(sandbox.SandboxError, match="copy failed"):
        executor.start(snapshot.WorkspaceSnapshot({}), tmp_path / "run")
    executor.close()
    assert docker.calls[-1][0] == ["docker", "rm", "--force", executor.name]


def test_unknown_tool_and_large_arguments_do_not_reach_docker(tmp_path):
    docker = DockerStub()
    executor = sandbox.SandboxExecutor(runner=docker)
    executor.start(snapshot.WorkspaceSnapshot({}), tmp_path / "run")
    count = len(docker.calls)
    assert executor.dispatch("host_exec", {}).startswith("Permission denied")
    assert executor.dispatch("bash", {"command": "x" * (129 * 1024)}).startswith("Error:")
    assert len(docker.calls) == count
    executor.close()


def test_native_worker_reuses_bounded_file_tools_and_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "base_tools", base_tools)
    monkeypatch.setattr(base_tools, "WORKDIR", tmp_path)
    (tmp_path / "source.txt").write_text("a\n中文证据\nc", encoding="utf-8")
    result = tool_worker.handle_request(
        {
            "action": "tool",
            "name": "read_file",
            "arguments": {"path": "source.txt", "start_line": 2, "limit": 1},
        }
    )
    assert "中文证据" in result["output"] and "start_line=3" in result["output"]
    escaped = tool_worker.handle_request(
        {
            "action": "tool",
            "name": "write_file",
            "arguments": {"path": "../escape.txt", "content": "bad"},
        }
    )
    assert escaped["output"].startswith("Error:")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_worker_initializes_from_tar_stdin_as_regular_writable_files(tmp_path, monkeypatch, capsys):
    archive_path = tmp_path / "source.tar"
    snapshot.WorkspaceSnapshot({"src/app.py": b"print('copy')"}).write_tar(archive_path)
    destination = tmp_path / "work"
    destination.mkdir()
    original_extract = tarfile.TarFile.extractall

    def extract_into_test_workspace(archive, path, *, filter):
        # 只替换实际目标目录；仍验证 Worker 的流式 tar、data filter 和真实解压。
        assert path == "/work" and filter == "data"
        return original_extract(archive, destination, filter=filter)

    monkeypatch.setattr(tarfile.TarFile, "extractall", extract_into_test_workspace)
    monkeypatch.setattr(tool_worker.sys, "argv", ["worker.py", "--init"])
    monkeypatch.setattr(
        tool_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(archive_path.read_bytes()))
    )
    tool_worker.main()
    assert "output" in json.loads(capsys.readouterr().out)
    copied = destination / "src/app.py"
    assert copied.read_bytes() == b"print('copy')"
    copied.write_bytes(b"modified")
    assert copied.read_bytes() == b"modified"


def test_worker_shell_uses_linux_and_timeout_is_fatal(monkeypatch):
    monkeypatch.setitem(sys.modules, "base_tools", base_tools)
    calls = []

    def completed(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=2, stdout="", stderr="bad option")

    monkeypatch.setattr(tool_worker.subprocess, "run", completed)
    assert tool_worker.run_bash("cmd") == "Error: command exited with code 2\nbad option"
    assert calls[0][0] == ["/bin/sh", "-c", "cmd"]
    assert calls[0][1]["cwd"] == "/work"

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("sh", 120)

    monkeypatch.setattr(tool_worker.subprocess, "run", timed_out)
    result = tool_worker.handle_request(
        {"action": "tool", "name": "bash", "arguments": {"command": "cmd"}}
    )
    assert "fatal" in result


def test_docker_transport_bounds_output_and_timeout(monkeypatch):
    # 只启动本地短命 Python 替身，不启动 Docker 或安装软件。
    assert sandbox.run_docker([sys.executable, "-c", "print('ok')"]).strip() == b"ok"
    monkeypatch.setattr(sandbox, "MAX_DOCKER_OUTPUT", 100)
    with pytest.raises(sandbox.SandboxError, match="输出超限"):
        sandbox.run_docker([sys.executable, "-c", "print('x' * 1000)"])
    with pytest.raises(sandbox.SandboxError, match="超过"):
        sandbox.run_docker([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.05)


def test_agent_loop_preserves_all_results_after_fatal_failure():
    events, saved, executed = [], [], []
    response = ModelResponse(
        content=[
            {"type": "tool_use", "id": str(index), "name": "bash", "input": {"command": "cmd"}}
            for index in range(3)
        ],
        model="offline",
        stop_reason="tool_use",
        usage=ModelUsage(),
    )

    def dispatch(name, arguments):
        executed.append(name)
        raise sandbox.SandboxError("container lost")

    with pytest.raises(sandbox.SandboxError, match="container lost"):
        chapter.agent_loop(
            [],
            create_message=lambda **kwargs: response,
            dispatch=dispatch,
            system=chapter.SYSTEM,
            hooks=chapter.Hooks(),
            emit=events.append,
            save_message=saved.append,
        )
    assert executed == ["bash"]
    assert [block["tool_use_id"] for block in saved[-1]["content"]] == ["0", "1", "2"]
    assert all(block["content"].startswith("Error:") for block in saved[-1]["content"])
    assert events[-1].data["reason"] == "sandbox_failed"


def test_main_automatically_uses_copy_persists_history_and_exports_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("original", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=host-secret", encoding="utf-8")
    manifest = tmp_path / "s12_skill_loading/skills/review/SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("---\nname: review\ndescription: 审查代码\n---\n技能正文", encoding="utf-8")
    docker = DockerStub()
    monkeypatch.setattr(
        chapter,
        "SandboxExecutor",
        lambda **kwargs: sandbox.SandboxExecutor(runner=docker, **kwargs),
    )
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    requests = []

    class Provider:
        model = "offline-test"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            assert "/work/s12_skill_loading/skills/review/SKILL.md" in kwargs["system"]
            assert str(tmp_path) not in kwargs["system"]
            if len(requests) == 1:
                content = [
                    {
                        "type": "tool_use",
                        "id": "write",
                        "name": "write_file",
                        "input": {"path": "README.md", "content": "changed"},
                    },
                    {
                        "type": "tool_use",
                        "id": "shell",
                        "name": "bash",
                        "input": {"command": "python -m pytest"},
                    },
                ]
            else:
                content = [{"type": "text", "text": "副本已修改"}]
            return ModelResponse(
                content,
                self.model,
                "tool_use" if len(requests) == 1 else "end_turn",
                ModelUsage(100, 10),
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    inputs = iter(["修改并测试副本", "q"])

    def user_input(prompt):
        assert "是否允许" not in prompt
        return next(inputs)

    monkeypatch.setattr("builtins.input", user_input)
    chapter.main(["--mode", "sandbox"])
    assert len(requests) == 2
    assert ".env" not in docker.files
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "original"
    diff_path = next(chapter.SANDBOX_ROOT.glob("*/changes.diff"))
    assert "+changed" in diff_path.read_text(encoding="utf-8")
    session = chapter.SessionManager.open(next(chapter.SESSION_ROOT.glob("*.jsonl")))
    assert session.build_context()[-1]["content"][0]["text"] == "副本已修改"
    assert session.session_stats().tool_calls == 2
    assert docker.calls[-1][0][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("answer", ["", "n", "y"])
def test_main_host_commands_confirm_apply_without_model_requests(tmp_path, monkeypatch, answer):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_bytes(b"original\n")
    docker = DockerStub()
    monkeypatch.setattr(
        chapter,
        "SandboxExecutor",
        lambda **kwargs: sandbox.SandboxExecutor(runner=docker, **kwargs),
    )
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    requests = []

    class Provider:
        model = "offline-test"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            if len(requests) % 2:
                content = [
                    {
                        "type": "tool_use",
                        "id": str(len(requests)),
                        "name": "write_file",
                        "input": {"path": "README.md", "content": f"round {len(requests)}\n"},
                    }
                ]
                stop_reason = "tool_use"
            else:
                content = [{"type": "text", "text": "done"}]
                stop_reason = "end_turn"
            return ModelResponse(content, self.model, stop_reason, ModelUsage(10, 10))

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    commands = ["first task", "/diff", "/apply", answer]
    if answer == "y":
        commands += ["/apply", "second task", "/diff", "/apply", "y"]
    commands += ["q"]
    inputs = iter(commands)

    def user_input(prompt):
        assert "是否允许" not in prompt
        command = next(inputs)
        if command == "q":
            # 检查发生在退出前；两轮应用使用同一个存活容器。
            expected = b"round 3\n" if answer == "y" else b"original\n"
            assert (tmp_path / "README.md").read_bytes() == expected
            assert not any(call[0][1] == "rm" for call in docker.calls)
        return command

    monkeypatch.setattr("builtins.input", user_input)
    chapter.main([])
    assert len(requests) == (4 if answer == "y" else 2)
    session = chapter.SessionManager.open(next(chapter.SESSION_ROOT.glob("*.jsonl")))
    user_texts = [
        message["content"]
        for message in session.build_context()
        if message["role"] == "user" and isinstance(message["content"], str)
    ]
    assert user_texts == (["first task", "second task"] if answer == "y" else ["first task"])
    report = next(chapter.SANDBOX_ROOT.glob("*/changes.diff"))
    assert (report.read_bytes() == b"") is (answer == "y")
    assert len(list(chapter.SANDBOX_ROOT.glob("*/apply-*.diff"))) == (2 if answer == "y" else 0)
    assert docker.calls[-1][0][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("arguments, code", [(["--help"], 0), (["--mode", "trusted"], 2)])
def test_cli_exits_before_creating_components(tmp_path, monkeypatch, arguments, code):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        chapter.main(arguments)
    assert error.value.code == code
    assert list(tmp_path.iterdir()) == []


def test_smoke_mode_never_initializes_model_or_loads_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    docker = DockerStub()
    monkeypatch.setattr(
        chapter,
        "SandboxExecutor",
        lambda **kwargs: sandbox.SandboxExecutor(runner=docker, **kwargs),
    )
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: pytest.fail("冒烟不需要 .env"))
    monkeypatch.setattr(
        chapter, "create_model_provider", lambda **kwargs: pytest.fail("冒烟不能初始化模型")
    )
    monkeypatch.setattr(
        chapter,
        "smoke_test",
        lambda executor: executor.dispatch(
            "write_file", {"path": "smoke.txt", "content": "copy only"}
        ),
    )
    chapter.main(["--smoke"])
    assert not (tmp_path / "smoke.txt").exists()
    assert next(chapter.SANDBOX_ROOT.glob("*/changes.diff")).is_file()
    assert not chapter.SESSION_ROOT.exists()


def test_main_cleanup_runs_after_model_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    docker = DockerStub()
    monkeypatch.setattr(
        chapter,
        "SandboxExecutor",
        lambda **kwargs: sandbox.SandboxExecutor(runner=docker, **kwargs),
    )
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)

    def failed(**kwargs):
        raise RuntimeError("provider configuration failed")

    monkeypatch.setattr(chapter, "create_model_provider", failed)
    with pytest.raises(RuntimeError, match="provider configuration"):
        chapter.main([])
    assert docker.calls[-1][0][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("failure_stage", ["export", "cleanup"])
def test_main_reports_export_or_cleanup_failure_instead_of_success(
    tmp_path, monkeypatch, failure_stage
):
    monkeypatch.chdir(tmp_path)
    docker = DockerStub()

    def failing_runner(arguments, **kwargs):
        if failure_stage == "cleanup" and arguments[1] == "rm":
            raise sandbox.SandboxError("cleanup failed")
        if failure_stage == "export" and arguments[1] == "exec" and arguments[-1] != "--init":
            return b'{"files":{"../host.txt":""}}'
        return docker(arguments, **kwargs)

    monkeypatch.setattr(
        chapter,
        "SandboxExecutor",
        lambda **kwargs: sandbox.SandboxExecutor(runner=failing_runner, **kwargs),
    )
    monkeypatch.setattr(chapter, "smoke_test", lambda executor: None)
    with pytest.raises(sandbox.SandboxError, match="未导出|手动清理"):
        chapter.main(["--smoke"])
    assert not (tmp_path.parent / "host.txt").exists()
    if failure_stage == "export":
        assert docker.calls[-1][0][1:3] == ["rm", "--force"]


@pytest.mark.skipif(
    os.getenv("SANDBOX_INTEGRATION") != "1",
    reason="显式 SANDBOX_INTEGRATION=1 才运行 Docker 集成测试",
)
def test_real_docker_isolation_and_unchanged_original(tmp_path):
    # s13-A 新增：只使用预先构建的本地镜像，绝不替用户 pull/build 或启动 Docker 服务。
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("original", encoding="utf-8")
    (source / ".env").write_text("HOST_SECRET=not copied", encoding="utf-8")
    before = snapshot.WorkspaceSnapshot.capture(source)
    executor = sandbox.SandboxExecutor()
    try:
        executor.start(before, tmp_path / "run")
        assert executor.dispatch("read_file", {"path": ".env"}).startswith("Error:")
        assert executor.dispatch(
            "write_file", {"path": "README.md", "content": "changed"}
        ).startswith("Wrote")
        assert executor.dispatch("read_file", {"path": "README.md"}) == "changed"
        identity = executor.dispatch(
            "bash", {"command": 'python -c "import os; print(os.getuid())"'}
        )
        assert identity.strip() == "65534"
        assert executor.dispatch("bash", {"command": "touch /opt/zero2pi/forbidden"}).startswith(
            "Error:"
        )
        network = (
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=1)\""
        )
        assert executor.dispatch("bash", {"command": network}).startswith("Error:")
        assert (
            executor.dispatch("bash", {"command": "ln -s /etc/passwd linked.txt"}) == "(no output)"
        )
        assert executor.dispatch("read_file", {"path": "linked.txt"}).startswith("Error:")
        settings = json.loads(
            executor.runner(
                ["docker", "inspect", "--format", "{{json .HostConfig}}", executor.name]
            )
        )
        assert settings["NetworkMode"] == "none"
        assert settings["ReadonlyRootfs"] is True and settings["Privileged"] is False
        assert not settings["Binds"]
        assert settings["PidsLimit"] == 64 and settings["Memory"] == 1024**3
        delivery = WorkspaceSync(source, executor, before, tmp_path / "changes.diff")
        delivery.preview()
        assert "+changed" in (tmp_path / "changes.diff").read_text(encoding="utf-8")
        assert (source / "README.md").read_text(encoding="utf-8") == "original"
        # s13-A 新增：两轮交付都发生在 close 之前，第二轮只比较第一次交付后的变化。
        assert delivery.apply() == 1
        assert (source / "README.md").read_text(encoding="utf-8") == "changed"
        executor.dispatch("write_file", {"path": "README.md", "content": "second change"})
        diff = delivery.preview()
        assert "-changed" in diff and "-original" not in diff
        assert delivery.apply() == 1
        assert executor.dispatch("read_file", {"path": "README.md"}) == "second change"
        assert delivery.preview() == ""
    finally:
        executor.close()
