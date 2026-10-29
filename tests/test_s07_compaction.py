"""验证压缩切点不拆开工具调用及结果。"""

from importlib import import_module

import pytest


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_preserves_tool_result_with_its_call(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    history = [
        messages.UserMessage("a" * 40),
        messages.AssistantMessage((messages.ToolCall("1", "read", {"path": "x"}),), "tool_use"),
        messages.ToolResultMessage("1", "read", "b" * 40),
        messages.AssistantMessage((messages.Text("c" * 40),)),
    ]
    entries = [{"id": str(i), "message": message} for i, message in enumerate(history)]
    cut = compaction.find_cut_point(entries, 15)
    assert cut.first_kept == 3
    assert cut.split_turn_start == 0
    cut = compaction.find_cut_point(entries, 21)
    assert cut.first_kept == 1
    assert cut.split_turn_start == 0


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_keeps_recent_user_turn_and_reports_no_work(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    entries = [
        {"id": "1", "message": messages.UserMessage("old" * 20)},
        {"id": "2", "message": messages.AssistantMessage((messages.Text("done"),))},
        {"id": "3", "message": messages.UserMessage("new" * 20)},
    ]
    assert compaction.find_cut_point(entries, 10) == compaction.CutPoint(2, None)
    assert compaction.find_cut_point(entries, 1000) is None
    with pytest.raises(ValueError):
        compaction.find_cut_point(entries, 0)


@pytest.mark.parametrize("package", ["pi_code_agent", "tutorials.s07_compaction"])
def test_cut_point_does_not_leave_an_orphaned_final_tool_result(package):
    messages = import_module(package + ".messages")
    compaction = import_module(package + ".compaction")
    entries = [
        {"id": "1", "message": messages.UserMessage("request")},
        {
            "id": "2",
            "message": messages.AssistantMessage(
                (messages.ToolCall("call", "read", {}),), "tool_use"
            ),
        },
        {"id": "3", "message": messages.ToolResultMessage("call", "read", "x" * 100)},
    ]
    # 预算落在末尾的工具结果，但它后面没有可用切点，不能把它单独留下。
    assert compaction.find_cut_point(entries, 1) is None
