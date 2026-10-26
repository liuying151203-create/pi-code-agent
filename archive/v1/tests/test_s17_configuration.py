"""s17：配置来源、脱敏校验、真实 SDK 请求参数及 CLI/教程接线。"""

import importlib
import os
import traceback
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import openai
import pytest
from anthropic import Anthropic

import tutorials.s16_agent_sdk.code as previous_chapter
import tutorials.s17_configuration.code as chapter
from pi_code_agent import (
    ModelConfig,
    ModelResponse,
    ModelUsage,
    RunConfig,
    cli,
    create_agent,
    models,
)
from pi_code_agent.compaction_base import CompactionPolicy
from pi_code_agent.config import load_environment, model_config_from_env, run_config_from_env
from pi_code_agent.skills import SkillCatalog


def environment(**overrides):
    """返回本用例自带的离线配置，不依赖开发者的环境和凭证。"""
    return {
        "MODEL_PROVIDER": "anthropic",
        "MODEL_ID": "offline",
        "ANTHROPIC_API_KEY": "offline-only",
        "MODEL_CONTEXT_WINDOW_TOKENS": "128000",
        **overrides,
    }


def test_environment_snapshot_has_no_global_writes_or_interpolation(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "MODEL_ID=file\nMODEL_API_KEY=${SECRET}\nMODEL_MAX_RETRIES=2\n", encoding="utf-8-sig"
    )
    process = {"MODEL_ID": "process", "MODEL_TIMEOUT_SECONDS": "19"}
    original = dict(os.environ)
    snapshot = load_environment(path, environ=process)
    assert snapshot["MODEL_ID"] == "process"
    assert snapshot["MODEL_API_KEY"] == "${SECRET}"
    assert snapshot["MODEL_MAX_RETRIES"] == "2"
    assert process == {"MODEL_ID": "process", "MODEL_TIMEOUT_SECONDS": "19"}
    assert dict(os.environ) == original


@pytest.mark.parametrize(
    "name,alias", [("MODEL_API_KEY", "OPENAI_API_KEY"), ("MODEL_BASE_URL", "OPENAI_BASE_URL")]
)
@pytest.mark.parametrize("file_alias,process_alias", [(False, True), (True, False), (True, True)])
def test_source_priority_applies_before_alias_priority(
    tmp_path, name, alias, file_alias, process_alias
):
    path = tmp_path / ".env"
    path.write_text(f"{alias if file_alias else name}=file\n", encoding="utf-8")
    result = load_environment(path, environ={alias if process_alias else name: "process"})
    assert result[name] == "process"
    assert alias not in result


def test_alias_within_one_source_and_empty_override(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MODEL_API_KEY=file\n", encoding="utf-8")
    result = load_environment(path, environ={"MODEL_API_KEY": "primary", "OPENAI_API_KEY": "alias"})
    assert result["MODEL_API_KEY"] == "primary"
    assert (
        model_config_from_env(
            {
                "MODEL_PROVIDER": "openai-compatible",
                "MODEL_ID": "offline",
                "MODEL_API_KEY": "",
                "OPENAI_API_KEY": "alias",
            }
        ).api_key
        == "alias"
    )
    result = load_environment(path, environ={"OPENAI_API_KEY": ""})
    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        model_config_from_env(
            {**result, "MODEL_PROVIDER": "openai-compatible", "MODEL_ID": "offline"}
        )


def test_missing_file_is_not_searched_upward_and_read_failure_propagates(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MODEL_ID=parent\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert load_environment(child / ".env", environ={}) == {}
    with pytest.raises(OSError):
        load_environment(child, environ={})


def test_defaults_explicit_run_options_and_compaction_boundaries():
    model = model_config_from_env(environment())
    assert (model.timeout_seconds, model.max_retries) == (60, 0)
    assert run_config_from_env({}) == RunConfig()
    run = run_config_from_env(
        environment(AGENT_MAX_ROUNDS="3", MODEL_MAX_TOKENS="123"), enable_compaction=True
    )
    assert run.max_rounds == 3 and run.max_tokens == 123
    assert run.compaction_policy == CompactionPolicy(128000)
    assert run.summary_max_tokens == 13107
    assert RunConfig(compaction_policy=CompactionPolicy(128000)).summary_max_tokens == 4096
    assert run_config_from_env({"MODEL_CONTEXT_WINDOW_TOKENS": "bad"}).compaction_policy is None
    with pytest.raises(FrozenInstanceError):
        model.model = "changed"


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODEL_TIMEOUT_SECONDS", "0"),
        ("MODEL_TIMEOUT_SECONDS", "nan"),
        ("MODEL_TIMEOUT_SECONDS", "inf"),
        ("MODEL_TIMEOUT_SECONDS", "-2"),
        ("MODEL_MAX_RETRIES", "-1"),
        ("MODEL_MAX_RETRIES", "1.5"),
        ("MODEL_ID", ""),
        ("ANTHROPIC_API_KEY", ""),
        ("MODEL_PROVIDER", "unknown"),
    ],
)
def test_invalid_model_environment_is_rejected(name, value):
    with pytest.raises(ValueError):
        model_config_from_env(environment(**{name: value}))


@pytest.mark.parametrize(
    "name,value",
    [
        ("AGENT_MAX_ROUNDS", "-1"),
        ("AGENT_MAX_ROUNDS", "1.5"),
        ("MODEL_MAX_TOKENS", "0"),
        ("MODEL_MAX_TOKENS", ""),
        ("MODEL_CONTEXT_WINDOW_TOKENS", "0"),
        ("MODEL_CONTEXT_WINDOW_TOKENS", "1000"),
        ("SESSION_COMPACTION_KEEP_RECENT_TOKENS", "999999"),
        ("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", "0"),
    ],
)
def test_invalid_run_environment_is_rejected(name, value):
    with pytest.raises(ValueError):
        run_config_from_env(environment(**{name: value}), enable_compaction=True)


def test_credentials_and_misplaced_values_are_not_in_repr_or_error():
    secret = "credential-that-must-not-appear"
    assert secret not in repr(ModelConfig(model="offline", api_key=secret))
    assert secret not in repr(ModelConfig(model="offline", auth_token=secret))
    for name in ("MODEL_TIMEOUT_SECONDS", "MODEL_MAX_RETRIES", "MODEL_PROVIDER"):
        with pytest.raises(ValueError) as error:
            model_config_from_env(environment(**{name: secret}))
        assert secret not in "".join(traceback.format_exception(error.value))
    with pytest.raises(ValueError) as error:
        ModelConfig(model="offline", api_key="offline", base_url=f"https://user:{secret}@host.test")
    assert secret not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("provider_name", ["anthropic", "openai-compatible"])
def test_explicit_model_config_reaches_real_sdk_request(provider_name, monkeypatch):
    captured = []

    def handle(request):
        captured.append(request)
        if provider_name == "anthropic":
            body = {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "offline",
                "content": [{"type": "text", "text": "完成"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        else:
            body = {
                "id": "chat-1",
                "object": "chat.completion",
                "model": "offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "完成"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        return httpx.Response(200, json=body)

    real_client = Anthropic if provider_name == "anthropic" else openai.OpenAI
    # 使用已安装 SDK 实际依赖的传输模块，兼容 httpx/httpx2，不额外安装测试依赖。
    client_module = importlib.import_module(real_client.__init__.__module__)
    httpx = getattr(client_module, "httpx2", None) or client_module.httpx

    def make_client(**kwargs):
        return real_client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(
        models if provider_name == "anthropic" else openai,
        "Anthropic" if provider_name == "anthropic" else "OpenAI",
        make_client,
    )
    for name in (
        "MODEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "MODEL_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "conflicting-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong.test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.test")
    config = ModelConfig(
        model="offline",
        provider=provider_name,
        api_key="explicit-key",
        base_url="https://explicit.test/v1",
        timeout_seconds=13,
        max_retries=0,
    )
    provider = models.create_model_provider(config=config)
    try:
        result = provider.complete(
            system="测试", messages=[{"role": "user", "content": "你好"}], max_tokens=99
        )
        assert result.content[0]["text"] == "完成"
        assert len(captured) == 1
        assert captured[0].url.host == "explicit.test"
        assert set(captured[0].extensions["timeout"].values()) == {13}
        header = "x-api-key" if provider_name == "anthropic" else "authorization"
        assert "explicit-key" in captured[0].headers[header]
        assert "conflicting-value" not in str(captured[0].headers)
        assert provider.client.max_retries == 0
    finally:
        provider.client.close()


def test_official_url_is_explicit_and_legacy_factory_remains_callable(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(models, "Anthropic", constructor)
    monkeypatch.setattr(
        os, "environ", environment(MODEL_TIMEOUT_SECONDS="40", MODEL_MAX_RETRIES="3")
    )
    models.create_model_provider(timeout_seconds=7, max_retries=0)
    options = constructor.call_args.kwargs
    assert options["timeout"] == 7 and options["max_retries"] == 0
    assert options["base_url"] == "https://api.anthropic.com"
    config = ModelConfig(model="offline", auth_token="explicit-token")
    models.create_model_provider(config=config)
    assert constructor.call_args.kwargs["api_key"] == ""
    assert constructor.call_args.kwargs["auth_token"] == "explicit-token"
    with pytest.raises(TypeError, match="混用"):
        models.create_model_provider(config=config, max_retries=0)


class OfflineProvider:
    """首个响应读取文件、后续响应完成，便于验证实际轮数接线。"""

    model = "offline"

    def __init__(self):
        self.calls = []

    def stream(self, *, on_text, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return ModelResponse(
            [
                {
                    "type": "tool_use",
                    "id": "read",
                    "name": "read_file",
                    "input": {"path": "note.txt"},
                }
            ],
            "offline",
            "tool_use",
            ModelUsage(20, 3),
        )

    def complete(self, **kwargs):
        pytest.fail("短任务不应触发摘要")


@pytest.mark.parametrize("factory", [create_agent, chapter.create_agent])
@pytest.mark.parametrize(
    "options",
    [
        {"max_rounds": 0},
        {"max_rounds": True},
        {"max_tokens": 0},
        {"summary_max_tokens": False},
        {"timeout_seconds": float("nan")},
    ],
)
def test_explicit_sdk_validation_precedes_session_creation(factory, options, tmp_path):
    records = tmp_path / "records"
    with pytest.raises(ValueError):
        factory(provider=OfflineProvider(), workspace=tmp_path, data_dir=records, **options)
    assert not records.exists()


@pytest.mark.parametrize("entry", [cli, chapter])
def test_cli_reads_startup_file_and_enforces_limit(entry, tmp_path, monkeypatch):
    startup = tmp_path / "host"
    startup.mkdir()
    workspace = tmp_path / "target"
    workspace.mkdir()
    (workspace / ".env").write_text("MODEL_ID=wrong-target\nAGENT_MAX_ROUNDS=0\n", encoding="utf-8")
    (workspace / "note.txt").write_text("实际文件证据", encoding="utf-8")
    (startup / ".env").write_text(
        "\n".join(
            f"{key}={value}"
            for key, value in environment(
                AGENT_MAX_ROUNDS="2",
                MODEL_MAX_TOKENS="89",
                MODEL_TIMEOUT_SECONDS="11",
                MODEL_MAX_RETRIES="0",
            ).items()
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(startup)
    monkeypatch.setattr(os, "environ", {"AGENT_MAX_ROUNDS": "1"})
    original_environment = dict(os.environ)
    provider = OfflineProvider()
    make_provider = Mock(return_value=provider)
    monkeypatch.setattr(entry, "create_model_provider", make_provider)
    monkeypatch.setattr("builtins.input", lambda prompt: "读取 note.txt")
    records = tmp_path / "records"
    with pytest.raises(RuntimeError, match="1 轮模型请求上限"):
        entry.main(["--workspace", str(workspace), "--data-dir", str(records)])
    assert len(provider.calls) == 1
    assert provider.calls[0]["max_tokens"] == 89
    actual = make_provider.call_args.kwargs["config"]
    assert actual.model == "offline" and actual.timeout_seconds == 11
    assert actual.max_retries == 0
    assert dict(os.environ) == original_environment
    paths = list(records.glob("sessions/*/*.jsonl"))
    assert len(paths) == 1 and "实际文件证据" in paths[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("entry", [cli, chapter])
@pytest.mark.parametrize(
    "name,value",
    [
        ("MODEL_TIMEOUT_SECONDS", "nan"),
        ("AGENT_MAX_ROUNDS", "-1"),
        ("MODEL_CONTEXT_WINDOW_TOKENS", "1000"),
    ],
)
def test_invalid_configuration_never_creates_client_or_records(
    entry, name, value, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", environment(**{name: value}))
    create_provider = Mock(side_effect=AssertionError("配置错误不能初始化客户端"))
    monkeypatch.setattr(entry, "create_model_provider", create_provider)
    with pytest.raises(ValueError):
        entry.main([])
    create_provider.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [cli, chapter])
@pytest.mark.parametrize("arguments,code", [("--help", 0), ("--mode invalid", 2)])
def test_help_and_invalid_arguments_precede_configuration(
    entry, arguments, code, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "load_environment", Mock(side_effect=AssertionError("不能读取配置")))
    with pytest.raises(SystemExit) as error:
        entry.main(arguments.split())
    assert error.value.code == code
    assert list(tmp_path.iterdir()) == []


def test_skill_scan_explicit_workspace_keeps_its_boundary(tmp_path, monkeypatch):
    host = tmp_path / "host"
    target = tmp_path / "target"
    host.mkdir()
    manifest = target / "skills" / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: review\ndescription: 测试项目技能\n---\n只读检查。", encoding="utf-8"
    )
    monkeypatch.chdir(host)
    with pytest.raises(ValueError, match="工作区"):
        SkillCatalog.scan(target / "skills")
    catalog = SkillCatalog.scan(target / "skills", workspace=target)
    assert catalog.skills[0].path == manifest.resolve()
    with pytest.raises(ValueError, match="工作区"):
        SkillCatalog.scan(host, workspace=target)


@pytest.mark.parametrize("entry", [previous_chapter, chapter])
def test_tutorial_can_start_for_another_workspace(entry, tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(host)
    monkeypatch.setattr(os, "environ", environment())
    if entry is previous_chapter:
        monkeypatch.setattr(entry, "load_dotenv", Mock())
    provider = OfflineProvider()
    monkeypatch.setattr(entry, "create_model_provider", Mock(return_value=provider))
    monkeypatch.setattr("builtins.input", lambda prompt: "q")
    entry.main(["--workspace", str(target), "--data-dir", str(tmp_path / "records")])
    assert provider.calls == []
