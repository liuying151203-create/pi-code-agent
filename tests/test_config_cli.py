"""配置和真实 CLI 入口验证，不触发任何网络请求。"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from pi_code_agent import ModelConfig, cli, load_model_config
from tutorials.s03_models import code as chapter
from tutorials.s04_events import code as event_chapter
from tutorials.s05_streaming import code as streaming_chapter


@pytest.mark.parametrize("mode", ["help", "demo"])
def test_offline_entry_does_not_import_model_sdks_or_dotenv(usage_api, tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("offline without model SDKs", encoding="utf-8")
    script = """
import importlib.abc, runpy, sys
from pathlib import Path
class MissingModelDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        name = fullname.split('.')[0]
        if name in {'openai', 'anthropic', 'dotenv'}:
            raise ModuleNotFoundError('blocked dependency: ' + name, name=name)
sys.meta_path.insert(0, MissingModelDependencies())
package = sys.argv.pop(2)
root = sys.argv.pop(1)
sys.path.insert(0, root)
sys.path.insert(0, str(Path(root) / 'src'))
runpy.run_module(package if package == 'pi_code_agent' else package + '.code', run_name='__main__')
"""
    arguments = (
        ["--help"]
        if mode == "help"
        else ["--demo", "--workspace", str(workspace), "--prompt", "读取 README.md"]
    )
    if mode == "demo":
        if hasattr(usage_api, "AgentSession"):
            arguments.append("--no-session")
        if hasattr(usage_api, "ExtensionRuntime"):
            arguments.append("--no-extensions")
        if hasattr(usage_api, "ResourceLoader"):
            arguments.append("--no-resources")
    package = usage_api.cli.__package__
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-B", "-c", script, str(root), package, *arguments],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("--demo" if mode == "help" else "offline without model SDKs") in result.stdout


@pytest.mark.parametrize("kind", ["anthropic", "openai-compatible"])
def test_model_construction_does_not_import_the_other_sdk(usage_api, tmp_path, kind):
    root = Path(__file__).resolve().parents[1]
    script = """
import asyncio, importlib, importlib.abc, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
kind = sys.argv[2]
blocked = 'openai' if kind == 'anthropic' else 'anthropic'
class MissingOtherSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == blocked:
            raise ModuleNotFoundError('blocked dependency: ' + blocked, name=blocked)
sys.meta_path.insert(0, MissingOtherSdk())
ModelConfig = importlib.import_module(sys.argv[3] + '.config').ModelConfig
create_model = importlib.import_module(sys.argv[3] + '.providers').create_model
async def main():
    model = create_model(ModelConfig(kind, 'test-model', 'fake-key'))
    await model.aclose()
asyncio.run(main())
assert blocked not in sys.modules
print('constructed and closed without the other SDK')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-B",
            "-c",
            script,
            str(root),
            kind,
            usage_api.cli.__package__,
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "constructed and closed" in result.stdout


def test_injected_clients_need_no_model_sdks_and_missing_sdk_is_reported(usage_api, tmp_path):
    """隔离进程禁止两个 SDK；注入可构造，创建自有客户端才报告缺失。"""
    root = Path(__file__).resolve().parents[1]
    script = """
import asyncio, importlib, importlib.abc, sys
from pathlib import Path
class MissingModelSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        name = fullname.split('.')[0]
        if name in {'openai', 'anthropic', 'dotenv'}:
            raise ModuleNotFoundError('blocked dependency: ' + name, name=name)
sys.meta_path.insert(0, MissingModelSdk())
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
config = importlib.import_module(sys.argv[2] + '.config')
providers = importlib.import_module(sys.argv[2] + '.providers')
async def main():
    for kind, factory, name in [
        ('anthropic', providers.AnthropicModel, 'anthropic'),
        ('openai-compatible', providers.OpenAIModel, 'openai'),
    ]:
        settings = config.ModelConfig(kind, 'test-model', 'fake')
        model = factory(settings, client=object())
        await model.aclose()
        try:
            factory(settings)
        except providers.ProviderError as error:
            assert name in str(error)
        else:
            raise AssertionError('missing SDK was not reported')
asyncio.run(main())
assert not {'anthropic', 'openai', 'dotenv'}.intersection(sys.modules)
print('injected clients OK; missing SDK reported')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-B",
            "-c",
            script,
            str(root),
            usage_api.cli.__package__,
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "injected clients OK" in result.stdout


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "wrong"},
        {"model": ""},
        {"api_key": ""},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"max_retries": -1},
        {"max_tokens": False},
        {"base_url": "file:///tmp/key"},
        {"token_parameter": "unknown"},
    ],
)
def test_invalid_config_fails_before_client_creation(kwargs):
    values = {"provider": "anthropic", "model": "m", "api_key": "fake"}
    values.update(kwargs)
    with pytest.raises(ValueError):
        ModelConfig(**values)


def test_environment_overrides_file_without_mutating_process(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "MODEL_PROVIDER=anthropic\nMODEL_ID=file\nANTHROPIC_API_KEY=fake\n", encoding="utf-8"
    )
    previous = dict(os.environ)
    config = load_model_config(path, environ={"MODEL_ID": "environment"})
    assert config.model == "environment"
    assert dict(os.environ) == previous
    assert "fake" not in repr(config)


def test_help_and_demo_do_not_create_model_or_load_credentials(tmp_path, monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("must not construct model"))
    monkeypatch.setattr(cli, "create_model", forbidden)
    monkeypatch.setattr(cli, "load_model_config", forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])
    assert stopped.value.code == 0
    (tmp_path / "README.md").write_text("offline project", encoding="utf-8")
    assert cli.main(["--demo", "--workspace", str(tmp_path), "--prompt", "read README"]) == 0
    output = capsys.readouterr().out
    assert "offline project" in output and "[本次用量]" not in output
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "module", [chapter, event_chapter, streaming_chapter], ids=["s03", "s04", "s05"]
)
def test_chapter_entry_runs_its_own_complete_loop(tmp_path, monkeypatch, module):
    (tmp_path / "README.md").write_text("project", encoding="utf-8")
    original = module.agent_loop
    calls = []

    async def recorded(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "agent_loop", recorded)
    assert module.main(["--demo", "--workspace", str(tmp_path), "--prompt", "read"]) == 0
    assert calls == [True]


def test_cli_displays_progress_before_tool_finishes_without_replaying_results(
    tmp_path, monkeypatch, capsys
):
    from dataclasses import replace

    (tmp_path / "README.md").write_text("actual file", encoding="utf-8")
    real_factory = cli.read_tool
    observed = []

    def checked_reader(workspace, *, resource_roots=()):
        reader = real_factory(workspace, resource_roots=resource_roots)

        async def execute(arguments):
            output = capsys.readouterr().out
            observed.append("[read 开始]" in output and "actual file" not in output)
            return await reader.execute(arguments)

        return replace(reader, execute=execute)

    monkeypatch.setattr(cli, "read_tool", checked_reader)
    assert cli.main(["--demo", "--workspace", str(tmp_path), "--prompt", "read"]) == 0
    assert observed == [True]
    output = capsys.readouterr().out
    assert output.count("[read 结果]") == 1
    assert "actual file" in output


def test_missing_config_is_reported_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    assert (
        cli.main(
            [
                "--env-file",
                str(tmp_path / "absent.env"),
                "--workspace",
                str(tmp_path),
                "--prompt",
                "hi",
            ]
        )
        == 1
    )
    assert "MODEL_ID" in capsys.readouterr().err


def test_invalid_turn_limit_fails_before_loading_config(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("must not load"))
    monkeypatch.setattr(cli, "load_model_config", forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--max-turns", "0", "--prompt", "hi"])
    assert stopped.value.code == 2
    forbidden.assert_not_called()


@pytest.fixture(
    params=[
        "pi_code_agent",
        "tutorials.s03_models",
        "tutorials.s04_events",
        "tutorials.s05_streaming",
        "tutorials.s06_sessions",
        "tutorials.s07_extensions",
        "tutorials.s08_hooks",
        "tutorials.s09_commands",
        "tutorials.s10_queues",
        "tutorials.s11_resources",
        "tutorials.s12_reload",
    ]
)
def usage_api(request):
    from conftest import load_runtime

    return load_runtime(request.param)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("usage_mode", ["known", "partial", "missing", "zero"])
def test_cli_totals_each_input_without_counting_old_history_or_stream_snapshots(
    usage_api, tmp_path, monkeypatch, capsys, stream, usage_mode
):
    """真实 CLI 连续运行两个工具闭环，用量只汇总本次的最终回答。"""
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from conftest import ScriptedModel

    api = usage_api
    supports_stream = hasattr(api.providers.AnthropicModel, "stream")
    if stream and not supports_stream:
        pytest.skip("s03/s04 尚未引入流式")
    usage_type = api.messages.Usage
    counts = [(11, 2), (7, 3), (13, 4), (17, 6)]
    usages = [usage_type(*count) for count in counts]
    if usage_mode == "partial":
        usages[0] = usages[2] = None
    elif usage_mode == "missing":
        usages = [None] * 4
    elif usage_mode == "zero":
        usages = [usage_type(0, 0)] * 4
    responses = [
        api.AssistantMessage(
            (api.ToolCall("read-first", "read", {"path": "README.md"}),), "tool_use", usages[0]
        ),
        api.AssistantMessage((api.Text("first answer"),), usage=usages[1]),
        api.AssistantMessage(
            (api.ToolCall("read-second", "read", {"path": "README.md"}),), "tool_use", usages[2]
        ),
        api.AssistantMessage((api.Text("second answer"),), usage=usages[3]),
    ]

    class Model(ScriptedModel):
        async def stream(self, **kwargs):
            response = await self.complete(**kwargs)
            partial = replace(response, stop_reason="pending")
            yield {"type": "start", "partial": partial}
            for index, block in enumerate(response.content):
                if isinstance(block, api.Text):
                    yield {
                        "type": "text_delta",
                        "content_index": index,
                        "delta": block.text,
                        "partial": partial,
                    }
            yield {"type": "done", "message": response}

    model = Model(*responses)
    model.aclose = AsyncMock()
    monkeypatch.setattr(api.cli, "create_model", lambda config: model)
    config = api.ModelConfig("anthropic", "test-model", "fake")
    monkeypatch.setattr(api.cli, "load_model_config", lambda path: config)
    inputs = iter(["first", "second", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    (tmp_path / "README.md").write_text("actual tool output", encoding="utf-8")
    arguments = ["--workspace", str(tmp_path)]
    if supports_stream and not stream:
        arguments.append("--no-stream")
    if hasattr(api, "AgentSession"):
        arguments.append("--no-session")
    if hasattr(api, "ExtensionRuntime"):
        arguments.append("--no-extensions")
    if hasattr(api, "ResourceLoader"):
        arguments.append("--no-resources")
    assert api.cli.main(arguments, loop=api.agent_loop) == 0
    output = capsys.readouterr().out
    statistics = [line for line in output.splitlines() if line.startswith("[本次用量]")]
    if usage_mode == "known":
        assert statistics == ["[本次用量] 输入 18，输出 5", "[本次用量] 输入 30，输出 10"]
    elif usage_mode == "partial":
        assert statistics == [
            "[本次用量] 输入 7，输出 3（部分请求未返回用量）",
            "[本次用量] 输入 17，输出 6（部分请求未返回用量）",
        ]
    elif usage_mode == "missing":
        assert statistics == ["[本次用量] 模型未返回用量"] * 2
    else:
        assert statistics == ["[本次用量] 输入 0，输出 0"] * 2
    assert len(model.calls) == 4
    assert output.count("[read 结果]") == 2
    model.aclose.assert_awaited_once()


@pytest.fixture(autouse=True)
def isolated_cli_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
