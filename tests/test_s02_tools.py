"""s02：真实 schema 校验、参数转换和文件执行边界。"""

import asyncio
from enum import Enum
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime
from pydantic import BaseModel, ConfigDict, Field


class Mode(str, Enum):
    FAST = "fast"
    SAFE = "safe"


class Item(BaseModel):
    count: int = Field(ge=1, le=3)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item] = Field(min_length=1)
    mode: Mode
    label: str = Field(min_length=2, max_length=6)
    limit: int = 2


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"items": [], "mode": "fast", "label": "good"},
        {"items": [{"count": 4}], "mode": "fast", "label": "good"},
        {"items": [{"count": 1}], "mode": "wrong", "label": "good"},
        {"items": [{"count": 1}], "mode": "fast", "label": "x"},
        {"items": [{"count": 1}], "mode": "fast", "label": "good", "extra": 1},
        [],
        "invalid JSON",
    ],
)
def test_schema_rejects_invalid_calls_before_handler(api, arguments):
    execute = AsyncMock()
    tool = api.Tool("validate", "test schema", execute, Arguments)
    result = asyncio.run(
        api.execute_tool(api.ToolCall("call", "validate", arguments), {tool.name: tool})
    )
    assert result.is_error
    execute.assert_not_called()


def test_conversion_defaults_and_optional_null_do_not_modify_raw_call(api):
    execute = AsyncMock(return_value=api.ToolResult("ok"))
    tool = api.Tool("validate", "test schema", execute, Arguments)
    raw = {"items": [{"count": "2"}], "mode": "safe", "label": "valid", "limit": None}
    call = api.ToolCall("call", "validate", raw)
    result = asyncio.run(api.execute_tool(call, {tool.name: tool}))
    assert not result.is_error
    assert execute.call_args.args[0]["items"] == [{"count": 2}]
    assert execute.call_args.args[0]["limit"] == 2
    assert call.arguments == raw
    assert tool.definition().parameters["properties"]["items"]["minItems"] == 1


def test_unknown_tool_and_invalid_handler_result_are_errors(api):
    result = asyncio.run(api.execute_tool(api.ToolCall("id", "missing", {}), {}))
    assert result.is_error
    tool = api.Tool("bad", "test", AsyncMock(return_value="not a ToolResult"))
    result = asyncio.run(api.execute_tool(api.ToolCall("id", "bad", {}), {tool.name: tool}))
    assert result.is_error


def test_model_can_correct_invalid_arguments_on_next_turn(api):
    class Integer(BaseModel):
        value: int

    execute = AsyncMock(return_value=api.ToolResult("fixed"))
    model = ScriptedModel(
        api.AssistantMessage((api.ToolCall("bad", "probe", {"value": "not integer"}),), "tool_use"),
        api.AssistantMessage((api.ToolCall("good", "probe", {"value": "3"}),), "tool_use"),
        api.AssistantMessage((api.Text("done"),)),
    )
    history = asyncio.run(
        api.agent_loop([], model=model, tools=[api.Tool("probe", "test", execute, Integer)])
    )
    results = [entry for entry in history if isinstance(entry, api.ToolResultMessage)]
    assert [entry.is_error for entry in results] == [True, False]
    execute.assert_awaited_once_with({"value": 3})


def test_read_tool_uses_workspace_and_reports_continuation(api, tmp_path):
    (tmp_path / "code.py").write_text("\n".join(str(i) for i in range(2002)), encoding="utf-8")
    tool = api.read_tool(tmp_path)
    first = asyncio.run(
        api.execute_tool(api.ToolCall("a", "read", {"path": "code.py"}), {"read": tool})
    )
    assert "offset=2001" in first.content
    rest = asyncio.run(
        api.execute_tool(
            api.ToolCall("b", "read", {"path": "code.py", "offset": 2001}), {"read": tool}
        )
    )
    assert rest.content == "2000\n2001"
    for path in ("../outside.txt", ".env", ".git/config"):
        result = asyncio.run(
            api.execute_tool(api.ToolCall("c", "read", {"path": path}), {"read": tool})
        )
        assert result.is_error


def test_read_tool_counts_utf8_bytes_without_skipping_lines(api, tmp_path):
    lines = ["中" * 1000 for _ in range(25)]
    (tmp_path / "code.txt").write_text("\n".join(lines), encoding="utf-8")
    tool = api.read_tool(tmp_path)
    first = asyncio.run(
        api.execute_tool(api.ToolCall("a", "read", {"path": "code.txt"}), {"read": tool})
    )
    body = first.content.split("\n\n[", 1)[0]
    assert len(body.encode("utf-8")) <= 50 * 1024
    count = len(body.splitlines())
    rest = asyncio.run(
        api.execute_tool(
            api.ToolCall("b", "read", {"path": "code.txt", "offset": count + 1}), {"read": tool}
        )
    )
    assert rest.content == "\n".join(lines[count:])


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s02_tools",
        "tutorials.s03_models",
        "tutorials.s04_events",
        "tutorials.s05_streaming",
        "tutorials.s06_sessions",
        "tutorials.s07_extensions",
        "tutorials.s08_hooks",
        "tutorials.s09_commands",
        "tutorials.s10_queues",
    ]
)
def api(request):
    return load_runtime(request.param)
