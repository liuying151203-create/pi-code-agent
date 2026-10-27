"""配置和真实 CLI 入口验证，不触发任何网络请求。"""

import os
from unittest.mock import Mock

import pytest

from pi_code_agent import ModelConfig, cli, load_model_config
from tutorials.s03_models import code as chapter
from tutorials.s04_events import code as event_chapter
from tutorials.s05_streaming import code as streaming_chapter


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
    assert "offline project" in capsys.readouterr().out
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

    def checked_reader(workspace):
        reader = real_factory(workspace)

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


@pytest.fixture(autouse=True)
def isolated_cli_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
