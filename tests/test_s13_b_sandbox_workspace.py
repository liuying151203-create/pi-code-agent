"""s13-B：离线检查共享挂载参数和直接写入；真实容器测试需显式开启。"""

import json
import os

import pytest

from s13_a_sandbox_copy.sandbox import SandboxError
from s13_b_sandbox_workspace import sandbox
from s13_b_sandbox_workspace.workspace_access import check_file_arguments, inspect_workspace


class SharedDockerStub:
    """模拟协议而不是容器隔离；写入只针对测试临时工作区。"""

    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.failure = False

    def __call__(self, arguments, *, data=None, timeout=30):
        self.calls.append((arguments, data))
        if arguments[1:3] == ["context", "inspect"]:
            return (
                b'[{"Endpoints":{"docker":{"Host":"npipe:////./pipe/dockerDesktopLinuxEngine"}}}]'
            )
        if arguments[1:3] == ["image", "inspect"]:
            return ("sha256:" + "a" * 64 + " linux").encode()
        if arguments[1] == "exec":
            request = json.loads(data)
            if self.failure:
                return b'{"fatal":"failed"}'
            if request["action"] == "probe":
                return b'{"output":"ready"}'
            args, name = request["arguments"], request["name"]
            denial = check_file_arguments(name, args, self.workspace)
            if denial:
                return json.dumps({"output": denial}).encode()
            if name == "write_file":
                path = self.workspace / args["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(args["content"], encoding="utf-8")
                output = "written"
            elif name == "read_file":
                output = (self.workspace / args["path"]).read_text(encoding="utf-8")
            else:
                output = "ok"
            return json.dumps({"output": output}).encode()
        return b"container"


def make_executor(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    if hasattr(os, "getuid"):
        monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
        monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)
    docker = SharedDockerStub(project)
    return sandbox.SharedWorkspaceExecutor(project, runner=docker), docker


def test_shared_mount_is_explicit_and_sensitive_paths_are_readonly_masked(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    (executor.workspace / ".env").write_text("secret", encoding="utf-8")
    (executor.workspace / ".git").mkdir()
    (executor.workspace / ".git/config").write_text("metadata", encoding="utf-8")
    (executor.workspace / ".env.example").write_text("public", encoding="utf-8")
    report = tmp_path / "report"
    executor.start(report)
    command = next(args for args, _ in docker.calls if args[1] == "run")
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert f"type=bind,source={executor.workspace},target=/work,bind-recursive=disabled" in mounts
    assert len(mounts) == 3
    assert all("readonly" in item for item in mounts if "target=/work," not in item)
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--pull") + 1] == "never"
    assert "--read-only" in command
    assert "ALL" in command
    assert not any("/work:rw" in item for item in command)
    assert not any("docker.sock" in item for item in command)
    assert (executor.workspace / ".env").read_text() == "secret"
    assert not (report / "source.tar").exists()
    assert all(data is None or data.startswith(b"{") for _, data in docker.calls)
    assert json.loads((report / "protection.json").read_text()) == [".env", ".git"]
    executor.close()
    assert docker.calls[-1][0][1:3] == ["rm", "--force"]


def test_file_writes_are_immediate_and_do_not_require_apply(tmp_path, monkeypatch):
    executor, _ = make_executor(tmp_path, monkeypatch)
    executor.start(tmp_path / "report")
    assert executor.dispatch("write_file", {"path": "new.py", "content": "x = 1\n"}) == "written"
    assert (executor.workspace / "new.py").read_text() == "x = 1\n"
    assert executor.dispatch("read_file", {"path": ".env"}).startswith("Permission denied")
    assert executor.dispatch("write_file", {"path": "../outside", "content": "x"}).startswith(
        "Permission denied"
    )
    executor.close()
    assert (executor.workspace / "new.py").exists()


def test_protection_changes_and_fatal_worker_errors_stop_without_fallback(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    executor.start(tmp_path / "report")
    docker.failure = True
    with pytest.raises(SandboxError, match="failed"):
        executor.dispatch("bash", {"command": "anything"})
    with pytest.raises(SandboxError, match="未就绪"):
        executor.dispatch("bash", {"command": "anything"})
    executor.close()


def test_new_credential_after_start_requires_restart(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    executor.start(tmp_path / "report")
    (executor.workspace / ".env").write_text("new host secret", encoding="utf-8")
    calls = len(docker.calls)
    with pytest.raises(SandboxError, match="保护路径已变化"):
        executor.dispatch("bash", {"command": "cat .env"})
    assert len(docker.calls) == calls
    executor.close()


def test_hardlinks_are_rejected_before_container_creation(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.link(outside, executor.workspace / "alias.txt")
    with pytest.raises(SandboxError, match="硬链接"):
        executor.start(tmp_path / "report")
    assert not any(args[1] == "run" for args, _ in docker.calls)
    assert outside.read_text() == "secret"


def test_path_commas_cannot_add_mount_options(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    path = executor.workspace / "name,readonly"
    path.mkdir()
    executor.workspace = path
    with pytest.raises(SandboxError, match="逗号"):
        executor.start(tmp_path / "report")
    assert not any(args[1] == "run" for args, _ in docker.calls)


def test_remote_docker_cannot_write_a_different_hosts_same_named_path(tmp_path, monkeypatch):
    executor, docker = make_executor(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    with pytest.raises(SandboxError, match="本机 Docker"):
        executor.start(tmp_path / "report")
    assert not any(args[1] == "run" for args, _ in docker.calls)
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize(
    "name", [".env", ".env.local", ".git/config", "folder/secret.pem", ".sessions/a"]
)
def test_protected_file_arguments_are_denied(tmp_path, name):
    assert check_file_arguments("write_file", {"path": name}, tmp_path).startswith(
        "Permission denied"
    )


def test_inspection_keeps_python_test_caches_usable(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/x.pyc").write_bytes(b"cache")
    assert inspect_workspace(tmp_path) == ()


@pytest.mark.skipif(os.getenv("SANDBOX_B_INTEGRATION") != "1", reason="显式开启真实 B 镜像测试")
def test_real_shared_container_writes_host_but_cannot_replace_protected_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("host-secret", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git/config").write_text("git-metadata", encoding="utf-8")
    executor = sandbox.SharedWorkspaceExecutor(project)
    try:
        executor.start(tmp_path / "report")
        executor.dispatch("write_file", {"path": "real.py", "content": "x = 1\n"})
        assert (project / "real.py").read_text() == "x = 1\n"
        assert executor.dispatch("read_file", {"path": ".env"}).startswith("Permission denied")
        for command in [
            "printf changed > .env",
            "rm .env",
            "mv .env renamed",
            "printf changed > .git/config",
        ]:
            assert executor.dispatch("bash", {"command": command}).startswith("Error:")
            assert (project / ".env").read_text() == "host-secret"
            assert (project / ".git/config").read_text() == "git-metadata"
        config = json.loads(sandbox.run_docker(["docker", "inspect", executor.name]))[0]
        assert config["HostConfig"]["NetworkMode"] == "none"
        assert config["HostConfig"]["ReadonlyRootfs"]
        assert len(config["Mounts"]) == 3
    finally:
        executor.close()
