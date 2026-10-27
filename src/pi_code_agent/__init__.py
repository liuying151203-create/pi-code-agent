"""从功能实现时就提供的公开异步接口，不导入教学或归档实现。"""

from pi_code_agent.config import ModelConfig, load_model_config

# s04 新增：事件与订阅随本章直接成为正式 SDK 接口。
from pi_code_agent.events import AgentEvent, EventBus, EventListener, EventListenerError
from pi_code_agent.loop import TurnLimitError, agent_loop
from pi_code_agent.messages import (
    AssistantMessage,
    Message,
    Text,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

# s05 新增：流式协议随本章向外部程序开放，旧 Model 接口保留。
from pi_code_agent.model import AssistantStreamEvent, Model, StreamingModel, ToolDefinition
from pi_code_agent.providers import AnthropicModel, OpenAIModel, ProviderError, create_model
from pi_code_agent.session import AgentSession, SessionStore
from pi_code_agent.tools import NoArguments, Tool, ToolResult

__version__ = "0.2.0"
__all__ = [
    "AgentEvent",
    "AgentSession",
    "AnthropicModel",
    "AssistantMessage",
    "AssistantStreamEvent",
    "EventBus",
    "EventListener",
    "EventListenerError",
    "Message",
    "Model",
    "ModelConfig",
    "NoArguments",
    "OpenAIModel",
    "ProviderError",
    "SessionStore",
    "StreamingModel",
    "Text",
    "Tool",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "ToolResultMessage",
    "TurnLimitError",
    "Usage",
    "UserMessage",
    "agent_loop",
    "create_model",
    "load_model_config",
]
