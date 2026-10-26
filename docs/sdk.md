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

## 观察运行进度

使用 `EventBus.subscribe(listener)` 注册同步或异步订阅器，再通过 `agent_loop(..., prompts=[新输入], events=events)` 运行；已有历史放在第一个参数中，不会重播。订阅器按顺序等待，`agent_end.messages` 仅包含本次新增消息，函数返回值仍是全部历史。

完整可运行示例见 [events_sdk.py](../examples/events_sdk.py)，设计和失败边界见 [s04](../tutorials/s04_events/README.md)。事件是观察数据，不会自动保存或加入模型上下文；回调得到独立副本。HTTP/协议异常会先追加失败消息并通知，再继续抛出原异常；订阅器异常使用 `EventListenerError` 单独报告，不保证后续事件完整。
