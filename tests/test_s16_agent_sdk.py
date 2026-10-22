"""s16：教学快照与正式 SDK 的离线行为契约及真实入口接线。"""

import runpy
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

import tutorials.s16_agent_sdk.code as chapter
from pi_code_agent import ModelResponse, ModelUsage, create_agent
from pi_code_agent.extensions.todo import register


def response(content=None, reason="end_turn"):
    """构造有明确停止状态和用量的离线响应。"""
    return ModelResponse(
        content=content if content is not None else [{"type": "text", "text": "完成"}],
        model="offline-s16",
        stop_reason=reason,
        usage=ModelUsage(100, 10),
    )


def tool_call(name, **arguments):
    """构造本测试实际使用的单次工具调用。"""
    return {"type": "tool_use", "id": "call-1", "name": name, "input": arguments}


class ScriptedProvider:
    """按顺序返回响应并保留请求快照，意外增加的请求会直接失败。"""

    model = "offline-s16"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, *, on_text, **kwargs):
        self.calls.append(deepcopy(kwargs))
        result = self.responses.pop(0)
        for block in result.content:
            if block["type"] == "text":
                on_text(block["text"])
        return result

    def complete(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return response([{"type": "text", "text": "保留用户目标和未完成事项。"}])


@pytest.fixture(params=[chapter.create_agent, create_agent], ids=["tutorial", "package"])
def agent_factory(request):
    return request.param


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "note.txt").write_text("工作区实例内容\n", encoding="utf-8")
    return folder


def test_explicit_dependencies_and_repeated_runs(agent_factory, workspace, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("注入 Provider 的 SDK 不得读取 .env 或自行创建模型")

    monkeypatch.setattr(chapter, "load_dotenv", unexpected)
    monkeypatch.setattr(chapter, "create_model_provider", unexpected)
    monkeypatch.delenv("MODEL_ID", raising=False)
    provider = ScriptedProvider(response(), response())
    events = []
    agent = agent_factory(
        provider=provider,
        workspace=workspace,
        data_dir=tmp_path / "records",
        system="调用方提供的系统说明",
        max_tokens=321,
        timeout_seconds=17,
    )
    assert provider.calls == []
    assert agent.session.path.is_file()
    try:
        agent.subscribe(events.append)
        first = agent.run("第一项任务")
        second = agent.run("第二项任务")
        assert [message["role"] for message in first] == ["user", "assistant"]
        assert [message["role"] for message in second] == ["user", "assistant", "user", "assistant"]
        assert provider.calls[1]["messages"][-1]["content"] == "第二项任务"
        assert provider.calls[0]["system"] == "调用方提供的系统说明"
        assert provider.calls[0]["max_tokens"] == 321
        assert {tool["name"] for tool in provider.calls[0]["tools"]} == {
            "read_file",
            "grep",
            "glob",
        }
        requests = [event for event in events if event.type == "model_request"]
        assert len(requests) == 2
        assert requests[0].data["timeout_seconds"] == 17
        assert "timeout" not in provider.calls[0]
    finally:
        agent.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        agent.run("继续")


def test_todo_state_is_saved_before_event_and_restored(agent_factory, workspace, tmp_path):
    provider = ScriptedProvider(
        response([tool_call("todo", action="add", text="学习 SDK")], "tool_use"), response()
    )
    agent = agent_factory(provider=provider, workspace=workspace, data_dir=tmp_path / "records")
    saved_states = []

    def observe(event):
        if event.type == "tool_result":
            saved_states.append(agent.session.latest_tool_details("todo"))

    try:
        agent.register_extension(register)
        agent.subscribe(observe)
        messages = agent.run("添加一个待办")
        state = agent.session.latest_tool_details("todo")
        session_path = agent.session.path
        assert saved_states == [state]
        assert state["todos"][0]["text"] == "学习 SDK"
        assert all("details" not in block for block in messages[2]["content"])
    finally:
        agent.close()

    restored_provider = ScriptedProvider(response())
    restored = agent_factory(
        provider=restored_provider,
        workspace=workspace,
        data_dir=tmp_path / "records",
        session_path=session_path,
    )
    try:
        restored.register_extension(register)
        assert restored.extensions.get_tool_details("todo") == state
        assert restored.session.build_context() == messages
        assert restored_provider.calls == []
    finally:
        restored.close()


def test_workspaces_and_record_access_are_isolated(agent_factory, workspace, tmp_path):
    original_cwd = Path.cwd()
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    (other_workspace / "note.txt").write_text("另一个工作区", encoding="utf-8")
    agents = []
    try:
        for folder in (workspace, other_workspace):
            agents.append(agent_factory(provider=ScriptedProvider(), workspace=folder))
        assert "工作区实例内容" in agents[0].extensions.call_tool("read_file", {"path": "note.txt"})
        assert "另一个工作区" in agents[1].extensions.call_tool("read_file", {"path": "note.txt"})
        assert agents[0].session.path.parent != agents[1].session.path.parent
        assert Path.cwd() == original_cwd
        assert (
            agents[0]
            .extensions.call_tool("read_file", {"path": ".pi-code-agent/secret.jsonl"})
            .startswith("Permission denied:")
        )
        assert (
            agents[0]
            .extensions.call_tool("write_file", {"path": "unexpected.txt", "content": "不能写入"})
            .startswith("Permission denied:")
        )
        assert not (workspace / "unexpected.txt").exists()
    finally:
        for agent in agents:
            agent.close()


@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_model_failure_propagates_without_dispatch(agent_factory, workspace, tmp_path, reason):
    provider = ScriptedProvider(response([tool_call("read_file", path="note.txt")], reason))
    agent = agent_factory(provider=provider, workspace=workspace, data_dir=tmp_path / "records")
    events = []
    try:
        agent.subscribe(events.append)
        with pytest.raises(RuntimeError, match="模型返回失败"):
            agent.run("失败的任务")
        assert not any(event.type == "tool_call" for event in events)
        assert events[-1].data["reason"] == "model_failed"
        assert all(message["role"] != "assistant" for message in agent.session.build_context())
        assert agent.session.session_stats().known_tokens == 110
    finally:
        agent.close()


def test_round_limit_saves_tool_result_and_releases_running_state(
    agent_factory, workspace, tmp_path
):
    provider = ScriptedProvider(
        response([tool_call("read_file", path="note.txt")], "tool_use"), response()
    )
    agent = agent_factory(
        provider=provider, workspace=workspace, data_dir=tmp_path / "records", max_rounds=1
    )
    try:
        with pytest.raises(RuntimeError, match="1 轮模型请求上限"):
            agent.run("读取文件")
        assert len(provider.calls) == 1
        assert "工作区实例内容" in agent.session.build_context()[-1]["content"][0]["content"]
        assert agent.run("请给出最终说明")[-1]["role"] == "assistant"
    finally:
        agent.close()


def test_running_instance_rejects_reentry_registration_and_close(
    agent_factory, workspace, tmp_path
):
    agent = agent_factory(
        provider=ScriptedProvider(response()), workspace=workspace, data_dir=tmp_path / "records"
    )

    def observe(event):
        if event.type == "agent_start":
            with pytest.raises(RuntimeError, match="已有任务"):
                agent.run("重入")
            with pytest.raises(RuntimeError, match="正在运行"):
                agent.register_extension(register)
            with pytest.raises(RuntimeError, match="同步任务尚未结束"):
                agent.close()

    try:
        agent.subscribe(observe)
        agent.run("正常任务")
    finally:
        agent.close()


def test_custom_backend_contract_and_empty_catalog(agent_factory, workspace, tmp_path):
    def dispatch(name, arguments):
        pytest.fail("显式空工具目录不得回退到默认后端")

    def permission(name, arguments):
        return "Permission denied: 自定义后端未开放工具"

    with pytest.raises(ValueError, match="同时提供"):
        agent_factory(provider=ScriptedProvider(), workspace=workspace, tools=[])
    provider = ScriptedProvider(response())
    agent = agent_factory(
        provider=provider,
        workspace=workspace,
        data_dir=tmp_path / "records",
        tools=[],
        dispatch=dispatch,
        permission_hook=permission,
    )
    try:
        agent.run("不需要工具")
        assert provider.calls[0]["tools"] == []
        assert agent.extensions.call_tool("read_file", {"path": "note.txt"}).startswith(
            "Permission denied:"
        )
    finally:
        agent.close()


def test_chapter_keeps_its_own_loop(workspace, tmp_path, monkeypatch):
    import pi_code_agent.agent as production

    def unexpected(*args, **kwargs):
        pytest.fail("章节快照须运行本章显式展开的循环")

    monkeypatch.setattr(production, "agent_loop", unexpected)
    agent = chapter.create_agent(
        provider=ScriptedProvider(response()), workspace=workspace, data_dir=tmp_path / "records"
    )
    try:
        assert agent.run("你好")[-1]["role"] == "assistant"
    finally:
        agent.close()


@pytest.mark.parametrize("arguments,code", [("--help", 0), ("--mode invalid", 2)])
def test_main_exits_before_configuration(arguments, code, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("帮助与非法参数应在模型配置之前退出")

    monkeypatch.setattr(chapter, "load_dotenv", unexpected)
    with pytest.raises(SystemExit) as error:
        chapter.main(arguments.split())
    assert error.value.code == code
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["none", "model", "extension"])
def test_main_uses_sdk_and_always_closes(workspace, tmp_path, monkeypatch, capsys, failure):
    provider = ScriptedProvider(response(reason="error" if failure == "model" else "end_turn"))
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: provider)
    for name, value in {
        "MODEL_CONTEXT_WINDOW_TOKENS": "128000",
        "SESSION_COMPACTION_RESERVE_TOKENS": "16384",
        "SESSION_COMPACTION_KEEP_RECENT_TOKENS": "20000",
        "SESSION_COMPACTION_SUMMARY_MAX_TOKENS": "4096",
        "MODEL_TIMEOUT_SECONDS": "60",
        "MODEL_MAX_RETRIES": "0",
        "AGENT_MAX_ROUNDS": "0",
    }.items():
        monkeypatch.setenv(name, value)
    prompts = iter(["你好", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(prompts))
    instances = []
    original_create = chapter.create_agent

    def capture_agent(**kwargs):
        agent = original_create(**kwargs)
        instances.append(agent)
        return agent

    monkeypatch.setattr(chapter, "create_agent", capture_agent)
    arguments = ["--workspace", str(workspace), "--data-dir", str(tmp_path / "records")]
    if failure == "extension":
        arguments.extend(["--extension", "invalid/module"])
        with pytest.raises(ValueError, match="Python 模块名"):
            chapter.main(arguments)
        assert provider.calls == []
    else:
        if failure == "model":
            with pytest.raises(RuntimeError, match="模型返回失败"):
                chapter.main(arguments)
        else:
            chapter.main(arguments)
        assert len(provider.calls) == 1
        assert "todo" in {tool["name"] for tool in provider.calls[0]["tools"]}
        assert "完成" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="已关闭"):
        instances[0].run("关闭后不能运行")


def test_external_program_calls_formal_sdk(capsys, monkeypatch, tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "sdk_basic.py"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runpy.run_path(str(example), run_name="__main__")
    output = capsys.readouterr().out
    assert "SDK 让程序复用同一套 Agent。" in output
    assert "模型上下文消息数：4" in output
    assert "tool_result" in output
    assert list(tmp_path.iterdir()) == []
