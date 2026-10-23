"""受 Pi 启发、使用 Python 独立实现的 Agent 与 Harness；非官方项目。"""

from pi_code_agent.agent import Agent, create_agent
from pi_code_agent.commands import CommandContext, CommandResult

# s17 新增：SDK 调用方可以直接构造配置，不必经由进程环境。
from pi_code_agent.config import ModelConfig, RunConfig
from pi_code_agent.events import AgentEvent, EventDispatcher
from pi_code_agent.extension_entries import CustomEntry, CustomMessageEntry
from pi_code_agent.hooks import Hooks
from pi_code_agent.loop import agent_loop
from pi_code_agent.models import ModelProvider, ModelResponse, ModelUsage
from pi_code_agent.registry import ExtensionAPI
from pi_code_agent.result import ToolResult
from pi_code_agent.session import SessionManager

__version__ = "0.1.0"
__all__ = [
    "Agent",
    "AgentEvent",
    "CommandContext",
    "CommandResult",
    "CustomEntry",
    "CustomMessageEntry",
    "EventDispatcher",
    "ExtensionAPI",
    "Hooks",
    "ModelConfig",
    "ModelProvider",
    "ModelResponse",
    "ModelUsage",
    "RunConfig",
    "SessionManager",
    "ToolResult",
    "agent_loop",
    "create_agent",
]
