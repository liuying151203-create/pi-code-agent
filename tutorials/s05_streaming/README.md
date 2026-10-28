# s05：文本流式响应

s04 能观察运行进度，但模型的文字要全部到齐才显示。本章让 Provider 直接消费网络流，把新收到的文字交给界面，同时保留一条完整回答。工具依然等最终回答到齐后执行。

## 运行

使用已配置的项目环境，不需要新增依赖：

```powershell
# 离线脚本流，工具实际读取练习项目
python -m tutorials.s05_streaming.code --demo --workspace example_project/mini_app --prompt "读取 README.md"
# 查看明确的 delta 分段和最终历史
python -m tutorials.s05_streaming.example
# 关闭流式，沿用完整响应
python -m tutorials.s05_streaming.code --demo --no-stream --workspace example_project/mini_app --prompt "读取 README.md"
```

填好已有 `.env` 后，去掉 `--demo` 即为真实模型流。

离线 DemoModel 按脚本模拟分段，不表示真实网络延迟。真实 Provider 直接消费服务端增量，没有先获取完整回答再拆段。

## 怎样从网络流走到界面

参考 Pi `e4c75a732`（0.85.1）的 `packages/agent/src/agent-loop.ts::streamAssistantResponse`：开始时放入当前回答，收到增量就替换这个位置，收到最终回答后再结束消息、执行工具。

本章 `StreamingModel.stream()` 返回可关闭的异步生成器，依次产生 `start`、`text_start/text_delta/text_end` 和 `done`。每个文本事件包含 `content_index` 和当前 `partial`；delta 是这一段新文字，partial 是截至此刻的回答快照。

```text
Provider.stream → start → 文本增量 → done
                  ↓          ↓        ↓
历史追加 pending 回答 → 替换同一位置 → 保存最终回答
message_start → message_update → message_end → 工具校验/执行
```

`agent_loop` 把文本事件放进 `message_update.assistant_message_event`；`message_update.message` 是当前快照。`pending` 只用于流中状态，不能作为最终回答执行工具。

新增组件的组装关系如下，箭头表示提供依赖：

```text
Provider ── model（stream 方法） ──> agent_loop
ConsoleDisplay ── subscribe(listener) ──> EventBus
EventBus ── events ──> agent_loop
```

实现对应本章 [model.py](model.py)、[providers.py](providers.py)、[code.py](code.py)、[events.py](events.py) 和 [cli.py](cli.py)。`ConsoleDisplay` 收到 delta 就输出，消息结束时只补未显示的后缀和换行，避免重复打印完整正文。

## 两种适配器各做什么

Anthropic 使用 SDK 的流上下文和消息累积器，把原始文本块事件转换为统一事件；最后校验 `message_stop`。SDK 可能宽松解析工具参数快照，本章会对原始 JSON 片段再做完整解析，解析失败交回已有工具校验，不借不完整快照执行。

流中收到 thinking、签名和脱敏块时，Provider 把它们保存在回答快照里；这些内容不会作为文本增量打印。最终回答会带着签名交给下一次请求；推理期间发生错误或取消，也保留已经收到的快照。

Chat Completions 从 `delta` 取文字，按工具 `index` 累积参数，校验 `finish_reason`，并继续读取末尾可能没有 choices 的用量块。字段核对见 [OpenAI 官方流式接口说明](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

兼容服务不支持 `stream_options` 时，可配置 `MODEL_STREAM_INCLUDE_USAGE=false`；SDK 对应 `ModelConfig(stream_include_usage=False)`。这只关闭用量请求参数，不关闭流式。额度字段继续由 `token_parameter` 明确选择。

## SDK 调用

完整响应的 `Model.complete()` 保留，原调用默认行为不变。使用流式时明确传 `stream=True`：

```python
import asyncio
from tutorials.s05_streaming.code import agent_loop
from tutorials.s05_streaming.demo import DemoModel, add_tool
from tutorials.s05_streaming.events import EventBus
from tutorials.s05_streaming.messages import UserMessage
from tutorials.s05_streaming.example import observe

history = []
events = EventBus()
events.subscribe(observe)
asyncio.run(
    agent_loop(
        history,
        model=DemoModel("add", {"a": 2, "b": 3}),
        tools=[add_tool()],
        prompts=[UserMessage("2 加 3")],
        events=events,
        stream=True,
    )
)
assert history[-1].stop_reason == "stop"
```

正式开发使用 `pi_code_agent`，见 [正式 SDK 示例](../../examples/streaming_sdk.py)。直接消费 `model.stream()` 时使用 `contextlib.aclosing`，让提前退出也关闭生成器；`agent_loop` 已负责这一点。`stream()` 只完成一次模型请求，工具闭环由循环负责。

## 失败和取消

缺少结束标记、网络断开或原生取消时，保留已收到的实际文字和用量，将同一条回答变为 `error/aborted`，关闭底层流，再通知结束并传播异常。不追加第二条模型回答，也不执行失败快照中的工具。

失败快照留在本地历史；再次请求时，Provider 按 Pi 的 `transformMessages` 过滤 error/aborted，pending 则直接拒绝重放。本章不自动重试。订阅器失败仍抛 `EventListenerError`，底层流会关闭，但余下通知不保证完整；二次取消可以打断清理通知。

## 测试

```powershell
python -m pytest tests/test_s05_streaming.py tests/test_config_cli.py tests/test_tutorial_snapshots.py
```

测试保留真实转换器和 Anthropic SDK 累积器，只替代网络边界。覆盖增量时序、历史替换、参数拼接、多个调用交错、用量尾块、截断和坏 JSON、连接关闭、早期取消、失败历史过滤及输出去重；不代表真实服务已联网验证。

## 参考与差异

- 已对齐：Pi 的开始/更新/完成顺序，增量携带当前快照，最终回答后执行工具，失败状态不执行，失败回答不重放。另参考 `packages/ai/src/api/anthropic-messages.ts`、`openai-completions.ts`、`transform-messages.ts` 及相关测试。
- 扩展与调整：Python 用异步生成器和 `aclosing` 代替 Pi 的 EventStream；网络异常和原生取消携带最新快照并继续传播。保留完整响应接口，CLI 可明确关闭流式；沿用 s04 的观察副本隔离。严格完整 JSON 校验与公开工具增量展示分开处理，本章不实现后者。
- 暂缓：工具增量展示、主动配置和展示推理、多模态、并行和自动恢复编号待定。本章保留当前上下文里的完整消息，会话与状态将在 s06 继续展开；不会自动迁入旧版会话功能。

来源与 MIT 声明见本章 [NOTICE](NOTICE.md)。
