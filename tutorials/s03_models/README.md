# s03：统一模型接口

Anthropic 和 OpenAI 兼容接口描述工具调用的方式不同。如果循环直接读取供应商字段，每换一个模型协议，都要修改循环。这一章把差异收进 Provider，让循环继续只认识统一消息。

## 先离线运行

```powershell
python -m tutorials.s03_models.code --demo --workspace example_project/mini_app --prompt "读取 README.md"
```

工具会实际读取练习项目文件，脚本模型根据结果回答。`--demo` 和 `--help` 都不需要模型配置。配置、Provider、文件工具和 CLI 均使用本目录快照；本章没有 s04 的事件组件，后续产品变化不会自动影响这里。

## 接入真实模型

在启动目录 `.env` 填写 `MODEL_PROVIDER`、`MODEL_ID` 和对应密钥，再运行：

```powershell
python -m tutorials.s03_models.code --workspace example_project/mini_app
# 正式入口提供相同能力
python -m pi_code_agent --workspace example_project/mini_app
```

当前支持 `anthropic` 和 `openai-compatible`。前者使用 `ANTHROPIC_API_KEY`，后者使用 `MODEL_API_KEY` 和可选 `MODEL_BASE_URL`。进程环境优先于文件，加载时不修改进程环境。

## 适配器做了什么

循环只调用 `await model.complete(...)`，收到 `AssistantMessage`。Provider 将内部消息转换为供应商请求，再把完整响应转换回来：

```text
内部消息 → Provider 转换请求 → await SDK 请求 → Provider 转换回答 → 内部消息
```

Anthropic 将工具结果放入 user 消息的 `tool_result` 块；Chat Completions 使用独立的 `tool` 角色和 `tool_call_id`。这些差异只出现在 本章 [providers.py](providers.py)，工具和循环保持不变。

停止状态也会统一：正常结束、工具调用、额度截断和模型失败分别处理。HTTP/协议错误抛 `ProviderError`，不会被包装成工具失败。输入/输出用量保存在回答中供入口显示，尚不计算费用。

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

Provider 创建的客户端在上下文退出时关闭；注入的客户端由调用方负责。本章入口选择 [code.py](code.py) 保留的完整无事件循环；正式 SDK 使用持续演进的 src/，本章的消息、工具、Provider 和终端组装独立保存。各章按阶段验证行为，不要求旧实现始终等于最新产品。

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
