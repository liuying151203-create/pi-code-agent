"""正式 SDK 的离线契约；不导入教程、不调用 API 或 Docker。"""

import pytest

from pi_code_agent import ModelResponse, ModelUsage, create_agent
from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.extensions.todo import register


def answer(text="完成", reason="end_turn"):
    return ModelResponse([{"type": "text", "text": text}], "offline", reason, ModelUsage(100, 10))


class OfflineProvider:
    model = "offline"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def stream(self, *, on_text, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0) if self.responses else answer()
        for block in result.content:
            if block["type"] == "text":
                on_text(block["text"])
        return result

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return answer("保留用户目标、代码证据和待完成事项。")


def make_agent(tmp_path, provider=None, **kwargs):
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    return create_agent(
        provider=provider or OfflineProvider(),
        workspace=workspace,
        data_dir=tmp_path / "records",
        **kwargs,
    )


def test_stream_restore_usage_and_closed_lifecycle(tmp_path):
    provider = OfflineProvider()
    agent = make_agent(tmp_path, provider)
    events = []
    agent.subscribe(events.append)
    messages = agent.run("你好")
    assert messages[-1]["content"][0]["text"] == "完成"
    assert {tool["name"] for tool in provider.calls[0]["tools"]} == {"read_file", "grep", "glob"}
    assert any(event.type == "assistant_delta" for event in events)
    restored = make_agent(tmp_path, session_path=agent.session.path)
    assert restored.session.build_context() == messages
    assert restored.session.session_stats() == agent.session.session_stats()
    agent.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        agent.run("继续")


def test_todo_state_survives_recreation(tmp_path):
    call = ModelResponse(
        [
            {
                "type": "tool_use",
                "id": "t1",
                "name": "todo",
                "input": {"action": "add", "text": "检查测试"},
            }
        ],
        "offline",
        "tool_use",
        ModelUsage(100, 10),
    )
    agent = make_agent(tmp_path, OfflineProvider([call, answer()]))
    agent.register_extension(register)
    agent.run("添加待办")
    saved = agent.session.latest_tool_details("todo")
    assert saved is not None
    restored = make_agent(tmp_path, session_path=agent.session.path)
    restored.register_extension(register)
    assert restored.extensions.get_tool_details("todo") == saved


def test_compaction_assembly_is_callable(tmp_path):
    agent = make_agent(tmp_path, compaction_policy=CompactionPolicy(128000))
    assert agent.run("你好")[-1]["role"] == "assistant"


def test_read_only_rejects_shell_and_writes(tmp_path):
    agent = make_agent(tmp_path)
    assert agent.extensions.call_tool("bash", {"command": "echo hi"}).startswith(
        "Permission denied:"
    )
    assert agent.extensions.call_tool("write_file", {"path": "x.py", "content": "x"}).startswith(
        "Permission denied:"
    )
    assert not (agent.workspace / "x.py").exists()


def test_two_workspaces_do_not_share_global_cwd(tmp_path):
    agents = []
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir()
        (folder / "file.py").write_text(str(index), encoding="utf-8")
        agents.append(
            create_agent(
                provider=OfflineProvider(), workspace=folder, data_dir=tmp_path / "records"
            )
        )
    assert "0" in agents[0].extensions.call_tool("read_file", {"path": "file.py"})
    assert "1" in agents[1].extensions.call_tool("read_file", {"path": "file.py"})
    assert agents[0].session.path.parent != agents[1].session.path.parent


def test_failed_response_not_success(tmp_path):
    agent = make_agent(tmp_path, OfflineProvider([answer("部分正文", "error")]))
    with pytest.raises(RuntimeError, match="模型返回失败"):
        agent.run("任务")
    assert all(message["role"] != "assistant" for message in agent.session.build_context())


def test_partial_backend_and_foreign_session_rejected(tmp_path):
    with pytest.raises(ValueError, match="同时提供"):
        make_agent(tmp_path, tools=[])
    with pytest.raises(ValueError, match="会话必须属于"):
        make_agent(tmp_path, session_path=tmp_path / "foreign.jsonl")
