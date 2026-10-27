"""新主线的离线模型替身，不导入归档实现。"""

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace


class ScriptedModel:
    """按顺序返回真实内部消息，并保存每次请求的独立快照。"""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return next(self.responses)


def load_runtime(package: str) -> SimpleNamespace:
    """给行为测试提供同一版本的组件，防止拿产品消息误测教学循环。

    输入：正式包或单个章节包名；输出：该版本实际拥有的类、方法和适配器。
    流程：加载本包消息/工具/循环 → 按章节阶段加载真实存在的组件；不做类型桥接。
    """
    stage = int(package.split(".")[-1][1:3]) if package.startswith("tutorials.") else 12
    loop_module = "code" if package.startswith("tutorials.") else "loop"
    exports = {}
    components = {
        "messages": ("UserMessage", "AssistantMessage", "Text", "ToolCall", "ToolResultMessage"),
        "tools": ("Tool", "ToolResult", "execute_tool"),
        loop_module: ("agent_loop", "TurnLimitError"),
    }
    if stage >= 2:
        components["file_tools"] = ("read_tool",)
        components["demo"] = ("AddArguments", "add_tool")
    if stage >= 3:
        components["cli"] = ()
        components["config"] = ("ModelConfig", "load_model_config")
        components["providers"] = ("AnthropicModel", "OpenAIModel", "ProviderError", "create_model")
    if stage >= 4:
        components["events"] = ("EventBus", "EventListenerError")
    if stage >= 6:
        components["session"] = ("AgentSession", "SessionStore", "encode_message", "decode_message")
    if stage >= 7:
        components["extensions"] = ("ExtensionAPI", "ExtensionRuntime", "discover_extensions")
    if stage >= 9:
        components["messages"] = (
            "UserMessage",
            "AssistantMessage",
            "Text",
            "ToolCall",
            "ToolResultMessage",
            "CustomMessage",
        )
    if stage >= 11:
        components["resources"] = (
            "ResourceLoader",
            "Skill",
            "PromptTemplate",
            "parse_frontmatter",
            "parse_command_args",
            "substitute_args",
        )
    for module_name, names in components.items():
        module = import_module(f"{package}.{module_name}")
        exports[module_name] = module
        exports.update({name: getattr(module, name) for name in names})
    return SimpleNamespace(**exports)


# 扩展与资源测试使用独立全局目录，不执行用户真实安装的插件。
import pytest


@pytest.fixture(autouse=True)
def isolated_agent_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODE_AGENT_HOME", str(tmp_path / "agent-home"))
