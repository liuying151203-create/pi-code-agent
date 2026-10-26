# SDK 用法

每章实现功能时就提供正式接口，外部程序只导入 `pi_code_agent`。当前是 0.2 异步接口，旧的同步 Agent 接口保留在 [v1 归档](../archive/README.md)。

## 离线运行

```python
import asyncio
from pi_code_agent import UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool

history = [UserMessage("2 加 3")]
asyncio.run(agent_loop(
    history, model=DemoModel("add", {"a": "2", "b": "3"}), tools=[add_tool()],
))
print(history[-1])
```

`agent_loop` 原地追加并返回同一个历史列表。默认不限制轮数；需要保护时显式传 `max_turns`。工具顺序执行；失败通过独立 `is_error` 标记回传，取消异常继续向外传播。模型返回 error/aborted 时保留回答并结束，调用方检查最终状态。

## 真实模型

```python
import asyncio
from pathlib import Path
from pi_code_agent import ModelConfig, UserMessage, agent_loop, create_model
from pi_code_agent.file_tools import read_tool

async def run():
    config = ModelConfig(provider="anthropic", model="your-model", api_key="your-key")
    reader = read_tool(Path("example_project/mini_app"))
    history = [UserMessage("读取 README.md")]
    async with create_model(config) as model:
        await agent_loop(history, model=model, tools=[reader], max_tokens=config.max_tokens)
    return history

history = asyncio.run(run())
```

SDK 不自动读取 `.env`。如有需要，调用 `load_model_config(明确路径, environ=映射)` 后显式使用配置。客户端由 Provider 创建时，在异步上下文退出时关闭；注入的客户端由调用方管理。

消息格式、工具参数模型和两种协议的转换分别见 [s01](../tutorials/s01_agent_loop/README.md)、[s02](../tutorials/s02_tools/README.md)、[s03](../tutorials/s03_models/README.md)。
