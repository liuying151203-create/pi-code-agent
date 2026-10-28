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

每个 `AssistantMessage.usage` 是一次模型请求的输入/输出 token；SDK 可遍历本次新增消息累加。CLI 的 `[本次用量]` 在循环返回后累计本次输入的全部回答，不包含旧历史；部分缺少用量会标明不完整。demo 没有真实 usage，不显示编造的消耗。目前没有会话累计、缓存或费用统计。

消息格式、工具参数模型和两种协议的转换分别见 [s01](../tutorials/s01_agent_loop/README.md)、[s02](../tutorials/s02_tools/README.md)、[s03](../tutorials/s03_models/README.md)。

## 观察运行进度

使用 `EventBus.subscribe(listener)` 注册同步或异步订阅器，再通过 `agent_loop(..., prompts=[新输入], events=events)` 运行；已有历史放在第一个参数中，不会重播。订阅器按顺序等待，`agent_end.messages` 仅包含本次新增消息，函数返回值仍是全部历史。

完整可运行示例见 [events_sdk.py](../examples/events_sdk.py)，设计和失败边界见 [s04](../tutorials/s04_events/README.md)。事件是观察数据，不会自动保存或加入模型上下文；回调得到独立副本。HTTP/协议异常会先记录失败消息并通知，再继续抛出原异常；订阅器异常使用 `EventListenerError` 单独报告，不保证后续事件完整。

## 文本流式

`agent_loop(..., stream=True)` 使用模型的 `stream()`，把文本片段转换为 `message_update`；其中 text_delta 的 `assistant_message_event.delta` 是新增文字，`message` 是当前快照。回答在历史中只占一个位置，pending 快照在完成时替换为最终回答，再继续工具闭环。默认 SDK 调用仍为完整响应，旧 Model 只实现 complete 也能使用。

可运行示例见 [streaming_sdk.py](../examples/streaming_sdk.py)，设计与验证见 [s05](../tutorials/s05_streaming/README.md)。正式 CLI 默认流式，`--no-stream` 可切回完整响应。直接消费 `model.stream()` 时，用 `contextlib.aclosing` 管理提前退出；循环内部已经负责关闭。

`ModelConfig.stream_include_usage` 控制 Chat 的用量参数，兼容服务可显式设为 False，对应环境变量 `MODEL_STREAM_INCLUDE_USAGE=false`。网络错误和取消保留已收到的内容与用量；失败快照不会重放给供应商，pending 历史拒绝请求。本章不自动重试，也不保证未实现的推理、多模态和工具增量展示。

依赖声明以本轮实际验证的 Anthropic 1.6.0、OpenAI 3.19.0 为 SDK 下界；本轮复用已有环境，未执行安装，也不声称更旧 SDK 支持这些流式字段。

## 会话

`AgentSession(model=..., tools=..., store=..., stream=True)` 持有当前消息，`await session.prompt(text)` 运行一次输入并返回历史副本；`session.subscribe` 订阅事件。`SessionStore.create(path, workspace)` 创建，`SessionStore.open(path, workspace)` 恢复。客户端仍由创建者关闭。CLI 默认保存到 `.sessions/<当前章>/<目标ID>/`，`--session PATH` 续接文件，`--no-session` 使用内存。见 [s06](../tutorials/s06_sessions/README.md)。

## 扩展

`ExtensionRuntime` 可 `await register(factory)` 或 `await load(paths)`；工厂接收 `ExtensionAPI`，调用 `register_tool`。传入 `AgentSession(extensions=runtime, builtin_tools=..., tools=...)` 后按 Pi 优先级组装工具，使用结束调用 `runtime.close()`。插件是可信代码，不是沙箱；项目自动加载需 --trust-project-extensions，--no-extensions 仍保留显式 -e。见 [s07](../tutorials/s07_extensions/README.md)。


## 扩展基座（s08–s12）

可运行的完整离线例子：`python examples/extension_foundation_sdk.py`。先阅读 [s06–s12 教程入口](../tutorials/README.md)，各章使用自己的冻结类型。

- `api.on` 注册 input/context/tool_call/tool_result 与生命周期观察；工具参数校验后才允许拦截。context 的临时修改不改历史，ToolResult.terminate 控制整批工具后的自动续轮。
- `api.register_command` 与 `register_flag/get_flag` 提供命令和严格参数。CLI 命令先于 input Hook，SDK 显式 `await session.run_command(text)`。
- `ctx.ui.notify` 只显示；`ctx.append_entry` 保存扩展状态；`await ctx.send_message(..., display=..., trigger_turn=..., deliver_as=...)` 明确选择模型消息、显示和触发回合。命令返回不自动交给模型。
- `session.steer`、`follow_up` 排队，默认一轮一条；`abort` 取消活跃任务，`await wait_for_idle` 等待末事件和保存完成。不要从活跃处理函数等待自己。
- `ResourceLoader(...).load()` 明确加载业务上下文、Skill 元数据和模板，再作为 resources 注入。SDK 不隐式扫描；Skill 资源读取范围可用 `read_tool(..., resource_roots=...)` 指定。
- `session.set_active_tools(names)` 在下一请求刷新定义和分发快照；`await session.reload()` 在空闲时验证候选再切换，保留历史。旧工具、API 和上下文随代次失效；`await session.aclose()` 只关闭扩展，模型客户端由创建者管理。

同一个 ExtensionRuntime 绑定一个会话。需要独立会话时建立独立运行时。状态文件为 Python 自有的线性 JSONL，不能直接导入 Pi 会话。资源/插件路径是可信配置；Python 扩展没有权限沙箱。重载回滚注册和资源集合，不能撤销插件自行产生的外部副作用。
