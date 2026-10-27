# s03：真实模型接入与协议适配

第一章已定义 `model.complete()`，第二章已完成工具参数校验。这一章把同一个调用接口接到真实模型：Provider 把内部消息翻译成服务请求，再把服务回答翻译回来，核心循环继续沿用。

## 先离线运行

```powershell
python -m tutorials.s03_models.code --demo --workspace example_project/mini_app --prompt "读取 README.md"
```

`DemoModel()` 按预设脚本请求读取 README，工具实际读取文件，再把内容交回脚本模型。`--demo` 和 `--help` 都不需要模型配置；这一模式不验证真实模型是否会选择工具。

## 接入真实模型

在启动目录 `.env` 填写 `MODEL_PROVIDER`、`MODEL_ID` 和对应密钥，再运行：

```powershell
python -m tutorials.s03_models.code --workspace example_project/mini_app
```

当前支持 `anthropic` 和 `openai-compatible`。前者使用 `ANTHROPIC_API_KEY`，后者使用 `MODEL_API_KEY` 和可选 `MODEL_BASE_URL`。进程环境优先于文件，加载时不修改进程环境。

## 适配器做了什么

[cli.py](cli.py) 负责组装，真实模式读取配置后创建模型，工具单独传给循环：

```python
config = load_model_config(options.env_file)
model = create_model(config)
```

`create_model()` 根据 provider 选择 `AnthropicModel` 或 `OpenAIModel`；模型名称、密钥等属于配置，工具名和工具参数由模型回答决定。

[code.py](code.py) 的循环仍只调用统一接口：

```python
response = await model.complete(
    system=system, messages=tuple(messages), tools=definitions, max_tokens=max_tokens
)
```

`messages` 是对话历史，`definitions` 是工具名称、说明和参数格式，不包含执行函数。Provider 将这些数据转换为供应商请求，再把完整响应转换回来：

```text
内部消息 → Provider 转换请求 → await SDK 请求 → Provider 转换回答 → 内部消息
```

[providers.py](providers.py) 中真正发起模型请求的位置是：

```python
# AnthropicModel.complete
raw = await self.client.messages.create(**request)
# OpenAIModel.complete
raw = await self.client.chat.completions.create(**request)
```

以“读取 README.md”为例：用户消息与 read 的工具定义发给模型 → 模型返回读取请求 → Provider 转成 `ToolCall` → 循环校验参数并执行 read → 工具结果进入历史 → 再次请求模型生成回答。真实模型也可以直接返回文字，此时不会执行工具。

Anthropic 将工具结果放入 user 消息的 `tool_result` 块；Chat Completions 使用独立的 `tool` 角色和 `tool_call_id`。适配器会将它们与原调用 ID 对应起来，循环只处理统一的 `ToolResultMessage`。

停止状态也会统一：正常结束、工具调用、额度截断和模型失败分别处理。失败回答留在本地历史，但后续请求会跳过它，避免重放未完成的工具调用。HTTP/协议错误抛 `ProviderError`，不会被包装成工具失败。输入/输出用量保存在回答中供入口显示，尚不计算费用。

## SDK 使用同一套组装

```python
from tutorials.s03_models.code import agent_loop
from tutorials.s03_models.config import ModelConfig
from tutorials.s03_models.providers import create_model

config = ModelConfig(provider="anthropic", model="your-model", api_key="your-key")


async def run(history, tools):
    async with create_model(config) as model:
        return await agent_loop(history, model=model, tools=tools, max_tokens=config.max_tokens)
```

SDK 不自动读取 `.env`。配置先校验，再创建客户端；超时和重试真正传给客户端，回答额度传入实际请求。对于兼容服务，`token_parameter` 显式选择 `max_tokens` 或 `max_completion_tokens`，不根据模型名猜测。

```text
ModelConfig ──config──> Provider ──model──> agent_loop
Tool ──tools───────────────────────────> agent_loop
```

Provider 创建的客户端在上下文退出时关闭；注入的客户端由调用方负责。

## 测试

```powershell
python -m pytest tests/test_s03_models.py tests/test_config_cli.py
```

测试模拟网络边界，保留真实转换器与循环：同一工具在两种协议下都能执行，调用 ID 正确对应，配置真正生效，取消和客户端关闭符合所有权。它们不代表真实服务已经联网验证。

## 参考与差异

参考 Pi `e4c75a732` 的 `packages/ai/src/api/anthropic-messages.ts`、`openai-completions.ts` 及相关测试，解决两种供应商协议下运行同一个循环的问题；Chat 字段核对 [OpenAI 官方接口说明](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。来源与许可见 本章 [NOTICE](NOTICE.md)。

- 已对齐：统一消息、工具调用关联、停止状态及协议边界；当前只实现文本和客户端 function 工具。
- 本项目调整：配置和客户端所有权显式传入，兼容接口的额度字段由调用方选择；本章测试验证请求参数与关闭行为，真实服务仍需使用自己的配置验证。
- 暂缓：流式在 s05 加入；推理、多模态和更多服务兼容项编号待定，届时补充对应内容块和服务测试。
