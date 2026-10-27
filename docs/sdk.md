# SDK 用法

每章实现功能时就提供正式接口，外部程序只导入 `pi_code_agent`。教程入口使用各章自己的冻结组件，产品 SDK 使用 src/ 的当前实现；两套消息和工具类型不混用。当前是 0.2 异步接口，旧的同步 Agent 接口保留在 [v1 归档](../archive/README.md)。

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

完整可运行示例见 [events_sdk.py](../examples/events_sdk.py)，设计和失败边界见 [s04](../tutorials/s04_events/README.md)。事件是观察数据，不会自动保存或加入模型上下文；回调得到独立副本。HTTP/协议异常会先记录失败消息并通知，再继续抛出原异常；订阅器异常使用 `EventListenerError` 单独报告，不保证后续事件完整。

## 文本流式

`agent_loop(..., stream=True)` 使用模型的 `stream()`，把文本片段转换为 `message_update`；其中 text_delta 的 `assistant_message_event.delta` 是新增文字，`message` 是当前快照。回答在历史中只占一个位置，pending 快照在完成时替换为最终回答，再继续工具闭环。默认 SDK 调用仍为完整响应，旧 Model 只实现 complete 也能使用。

可运行示例见 [streaming_sdk.py](../examples/streaming_sdk.py)，设计与验证见 [s05](../tutorials/s05_streaming/README.md)。正式 CLI 默认流式，`--no-stream` 可切回完整响应。直接消费 `model.stream()` 时，用 `contextlib.aclosing` 管理提前退出；循环内部已经负责关闭。

`ModelConfig.stream_include_usage` 控制 Chat 的用量参数，兼容服务可显式设为 False，对应环境变量 `MODEL_STREAM_INCLUDE_USAGE=false`。网络错误和取消保留已收到的内容与用量；失败快照不会重放给供应商，pending 历史拒绝请求。本章不自动重试，也不保证未实现的推理、多模态和工具增量展示。

依赖声明以本轮实际验证的 Anthropic 1.6.0、OpenAI 3.19.0 为 SDK 下界；本轮复用已有环境，未执行安装，也不声称更旧 SDK 支持这些流式字段。
