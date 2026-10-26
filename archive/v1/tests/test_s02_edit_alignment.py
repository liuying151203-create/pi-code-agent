"""对照 Pi tools.test.ts 验证编辑行为，覆盖教程、函数接口和实例后端。"""

import pytest

import tutorials.s02_tool_use.code as chapter
from pi_code_agent import permissions, tools
from pi_code_agent.local_tools import LocalTools


@pytest.fixture(params=["chapter", "functions", "permissions", "instance"])
def edit(request, tmp_path, monkeypatch):
    if request.param == "instance":
        return LocalTools(tmp_path, "default").edit_file
    module = {"chapter": chapter, "functions": tools, "permissions": permissions}[request.param]
    monkeypatch.setattr(module, "WORKDIR", tmp_path)
    return module.run_edit


def test_formal_tool_catalog_advertises_the_actual_batch_interface():
    for catalog in (chapter.TOOLS, tools.TOOLS, permissions.TOOLS):
        definition = next(tool for tool in catalog if tool["name"] == "edit_file")
        assert definition["input_schema"]["required"] == ["path", "edits"]


@pytest.mark.parametrize("old,new", [("same", "new"), ("", "new"), ("same same", "same same")])
def test_ambiguous_empty_or_unchanged_edit_does_not_write(edit, tmp_path, old, new):
    target = tmp_path / "file.txt"
    original = b"same same"
    target.write_bytes(original)
    try:
        result = edit("file.txt", old, new)
    except ValueError:
        pass
    else:
        assert result.startswith("Error:")
    assert target.read_bytes() == original


def test_batch_edits_match_original_and_preserve_bom_crlf(edit, tmp_path):
    target = tmp_path / "file.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\r\n")
    edit(
        "file.txt",
        edits=[{"oldText": "alpha", "newText": "beta"}, {"oldText": "beta", "newText": "BETA"}],
    )
    assert target.read_bytes() == b"\xef\xbb\xbfbeta\r\nBETA\r\ngamma\r\n"


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [{"oldText": "alpha", "newText": "A"}, {"oldText": "missing", "newText": "B"}],
        [{"oldText": "alpha", "newText": "A"}, {"oldText": "alpha beta", "newText": "B"}],
    ],
)
def test_invalid_batch_never_partially_writes(edit, tmp_path, edits):
    target = tmp_path / "file.txt"
    target.write_text("alpha beta", encoding="utf-8")
    try:
        output = edit("file.txt", edits=edits)
    except ValueError:
        pass
    else:
        assert output.startswith("Error:")
    assert target.read_text(encoding="utf-8") == "alpha beta"


def test_mixed_fuzzy_and_exact_edits_on_one_line_preserve_other_lines(edit, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("keep   \n‘one’ and beta  \nother — text   \n", encoding="utf-8")
    edit(
        "file.txt",
        edits=[{"oldText": "'one'", "newText": "'ONE'"}, {"oldText": "beta", "newText": "B"}],
    )
    assert target.read_text(encoding="utf-8") == "keep   \n'ONE' and B\nother — text   \n"


def test_fuzzy_edit_only_rewrites_touched_lines(edit, tmp_path):
    target = tmp_path / "file.txt"
    original = "keep trailing   \nprint(‘hello’)  \nother — text   \n"
    target.write_text(original, encoding="utf-8")
    edit("file.txt", "print('hello')", "print('updated')")
    assert (
        target.read_text(encoding="utf-8")
        == "keep trailing   \nprint('updated')\nother — text   \n"
    )
