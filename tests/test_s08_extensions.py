"""s08：真实可信工厂执行、注册事务、发现顺序、覆盖规则与模块清理。"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s08_extensions",
        "tutorials.s09_hooks",
        "tutorials.s10_commands",
        "tutorials.s11_queues",
        "tutorials.s12_resources",
        "tutorials.s13_reload",
    ]
)
def api(request):
    return load_runtime(request.param)


@pytest.fixture
def runtime(api):
    value = api.ExtensionRuntime()
    yield value
    value.close()


def test_discovery_is_ordered_nonrecursive_and_deduplicated(api, tmp_path):
    project = tmp_path / ".pi-code-agent/extensions"
    project.mkdir(parents=True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    for file in [project / "b.py", project / "a.py", global_dir / "global.py"]:
        file.write_text("def setup(api): pass\n", encoding="utf-8")
    package = project / "package"
    package.mkdir()
    (package / "index.py").write_text("def setup(api): pass", encoding="utf-8")
    (package / "helper.py").write_text('raise Exception("not an entry")', encoding="utf-8")
    expected = [project / "a.py", project / "b.py", package / "index.py", global_dir / "global.py"]
    assert (
        api.discover_extensions(tmp_path, global_dir=global_dir, paths=[project / "a.py"])
        == expected
    )
    assert api.discover_extensions(tmp_path / "missing") == []
    assert api.discover_extensions(tmp_path, paths=[Path("missing.py")], project=False) == [
        tmp_path / "missing.py"
    ]


def test_factory_commit_failure_rolls_back_and_escaped_api_stays_invalid(api, runtime):
    escaped = []

    def fail(extension):
        escaped.append(extension)
        extension.register_tool(api.Tool("bad", "bad", AsyncMock()))
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError, match="factory failed"):
        asyncio.run(runtime.register(fail))
    assert runtime.resolve_tools() == []
    assert runtime.extensions == ()
    with pytest.raises(RuntimeError):
        escaped[0].register_tool(api.Tool("late", "late", AsyncMock()))


def test_priority_is_last_inside_first_between_then_sdk_overrides(api, runtime):
    def factory(label):
        def setup(extension):
            extension.register_tool(
                api.Tool("same", "first", AsyncMock(return_value=api.ToolResult("old")))
            )
            extension.register_tool(
                api.Tool("same", label, AsyncMock(return_value=api.ToolResult(label)))
            )

        return setup

    asyncio.run(runtime.register(factory("project"), source="project"))
    asyncio.run(runtime.register(factory("global"), source="global"))
    builtin = api.Tool("same", "builtin", AsyncMock(return_value=api.ToolResult("builtin")))
    resolved = runtime.resolve_tools([builtin])
    assert resolved[0].description == "project"
    assert runtime.tool_sources()["same"] == "project"
    sdk = api.Tool("same", "sdk", AsyncMock(return_value=api.ToolResult("sdk")))
    assert runtime.resolve_tools([builtin], sdk_tools=[sdk])[0] is sdk
    assert api.AgentSession(model=ScriptedModel(), builtin_tools=[builtin], tools=[sdk]).tools == (
        sdk,
    )


def test_successful_file_relative_imports_execute_and_owned_modules_are_removed(
    api, runtime, tmp_path
):
    package = tmp_path / "package"
    package.mkdir()
    (package / "helper.py").write_text('VALUE = "relative import"\n', encoding="utf-8")
    entry = package / "index.py"
    entry.write_text(
        'from .helper import VALUE\nasync def setup(api):\n    async def execute(args):\n        return api.ToolResult(VALUE)\n    api.register_tool(api.Tool("hello", "hello", execute))\n',
        encoding="utf-8",
    )
    before = set(sys.modules)
    asyncio.run(runtime.load([entry, entry]))
    assert not runtime.diagnostics
    assert len(runtime.extensions) == 1
    owned = set(sys.modules) - before
    assert any(name.endswith(".helper") for name in owned)
    model = ScriptedModel(
        api.AssistantMessage((api.ToolCall("a", "hello", {}),), "tool_use"),
        api.AssistantMessage((api.Text("done"),)),
    )
    session = api.AgentSession(model=model, extensions=runtime)
    history = asyncio.run(session.prompt("hello"))
    assert history[-2].content == "relative import"
    tool = session.tools[0]
    runtime.close()
    assert not any(
        name.startswith("_pi_code_agent_extension_") and name in owned for name in sys.modules
    )
    result = asyncio.run(api.execute_tool(api.ToolCall("closed", "hello", {}), {"hello": tool}))
    assert result.is_error and "失效" in result.content


def test_bad_file_isolated_good_file_still_loads_and_failed_namespace_removed(
    api, runtime, tmp_path
):
    bad = tmp_path / "bad.py"
    good = tmp_path / "good.py"
    bad.write_text(
        'def setup(api):\n    api.register_tool(api.Tool("bad","bad",lambda args: None))\n    raise ValueError("broken")\n',
        encoding="utf-8",
    )
    good.write_text(
        'def setup(api):\n    async def execute(args): return api.ToolResult("good")\n    api.register_tool(api.Tool("good","good",execute))\n',
        encoding="utf-8",
    )
    asyncio.run(runtime.load([bad, good, tmp_path / "missing.py"]))
    assert [tool.name for tool in runtime.resolve_tools()] == ["good"]
    assert len(runtime.diagnostics) == 2
    assert "broken" in runtime.diagnostics[0]["error"]
    assert not any(
        getattr(module, "__file__", None) == str(bad)
        for module in tuple(sys.modules.values())
        if module
    )


def test_async_factory_cancelled_before_commit_leaves_no_registration(api, runtime):
    async def setup(extension):
        extension.register_tool(api.Tool("bad", "bad", AsyncMock()))
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.register(setup))
    assert runtime.resolve_tools() == []


def test_cli_explicit_extension_works_with_auto_discovery_disabled(
    api, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    entry = tmp_path / "hello.py"
    entry.write_text(
        'def setup(api):\n    async def execute(args): return api.ToolResult("extension result")\n    api.register_tool(api.Tool("hello","hello",execute))\n',
        encoding="utf-8",
    )
    assert (
        api.cli.main(
            [
                "--demo",
                "--demo-tool",
                "hello",
                "--no-session",
                "--no-extensions",
                "-e",
                str(entry),
                "--workspace",
                str(tmp_path),
                "--prompt",
                "hello",
            ],
            loop=api.agent_loop,
        )
        == 0
    )
    assert "extension result" in capsys.readouterr().out


def test_project_discovery_requires_explicit_trust_flag(api, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / ".pi-code-agent/extensions"
    project.mkdir(parents=True)
    entry = project / "hello.py"
    entry.write_text(
        'def setup(api):\n    async def execute(args): return api.ToolResult("trusted result")\n    api.register_tool(api.Tool("hello","hello",execute))\n',
        encoding="utf-8",
    )
    args = [
        "--demo",
        "--demo-tool",
        "hello",
        "--no-session",
        "--workspace",
        str(tmp_path),
        "--prompt",
        "hello",
    ]
    assert api.cli.main(args, loop=api.agent_loop) == 0
    assert "trusted result" not in capsys.readouterr().out
    assert api.cli.main([*args, "--trust-project-extensions"], loop=api.agent_loop) == 0
    assert "trusted result" in capsys.readouterr().out


def test_extension_has_native_module_spec_and_can_read_its_package_resources(
    api, runtime, tmp_path
):
    (tmp_path / "message.txt").write_text("package data", encoding="utf-8")
    entry = tmp_path / "index.py"
    entry.write_text(
        "from importlib.util import find_spec\nfrom importlib.resources import files\n"
        "def setup(api):\n"
        "    assert find_spec(__name__).origin == __file__\n"
        '    value = files(__package__).joinpath("message.txt").read_text(encoding="utf-8")\n'
        "    async def execute(args): return api.ToolResult(value)\n"
        '    api.register_tool(api.Tool("data","data",execute))\n',
        encoding="utf-8",
    )
    asyncio.run(runtime.load([entry]))
    assert runtime.diagnostics == []
    result = asyncio.run(
        api.execute_tool(
            api.ToolCall("id", "data", {}), {tool.name: tool for tool in runtime.resolve_tools()}
        )
    )
    assert result.content == "package data"
