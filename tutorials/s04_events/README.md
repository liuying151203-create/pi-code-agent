# s04：运行事件与订阅

前三章能完成任务，但界面要等整个循环返回，才能显示结果。如果一个工具执行很久，用户只能等。这一章让循环在关键位置发事件，界面和外部程序订阅这些事件，就能看到当前进度。

事件只描述已经发生的过程，不会自动写入模型上下文，也不会保存成日志。本章仍一次接收完整模型回答；逐字输出在 s05 实现。

## 先运行

沿用项目根目录已配置的环境，不需要新增依赖或模型密钥：

```powershell
python -m tutorials.s04_events.code --demo --workspace example_project/mini_app --prompt "读取 README.md"
```

终端先显示 `[read 开始]`，读完显示实际文件内容，再显示模型回答。正式入口 `python -m pi_code_agent` 使用相同订阅方式。本章完整循环在 [code.py](code.py)，正式实现在 [loop.py](../../src/pi_code_agent/loop.py)。

想直接看到事件名称和顺序，可以运行：

```powershell
python examples/events_sdk.py
```

这个离线 SDK 示例实际计算 `2 + 3`，同时打印事件名称与工具结果 `5`。

## Pi 怎样通知外部程序

参考 Pi `e4c75a732`（0.85.1）的 `packages/agent/README.md` 中 Event Flow，以及 `agent-loop.ts` 的 `runAgentLoop/runLoop`、`agent.ts` 的 `subscribe/processEvents`。

Pi 在循环边界调用 `await emit(event)`。一次回合包含一次模型回答和它请求的整批工具，工具结果交回模型后才开始下一回合。订阅器按注册顺序执行，返回 Promise 时必须等它完成；最后的 `agent_end` 订阅器也处理完，运行调用才结束。

本章对应过程如下，`message_start/end` 表示连续的两个事件：

```text
agent_start
  turn_start
    新输入 message_start/end
    模型回答 message_start/end
    tool_execution_start → 查找/校验/执行 → tool_execution_end
    工具结果 message_start/end
  turn_end
  turn_start
    模型最终回答 message_start/end
  turn_end
agent_end
```

`tool_execution_start` 表示开始处理这次尝试，参数未通过校验、工具不存在或回答被截断，也会以 `is_error=True` 的结束事件收口。它不保证工具函数已经执行。

## 本项目的三个接点

- `EventBus.subscribe(listener)` 注册函数，返回取消订阅函数；同一函数不会重复注册，取消可以重复调用。
- `agent_loop(..., events=events)` 在实际边界发送事件，等待订阅器，自己不打印。
- CLI 的 `display_event` 订阅工具开始和消息结束，负责显示实际内容。模型消息里不会出现 `[read 开始]` 这类界面文字。

组装关系中的箭头表示提供依赖：

```text
display_event ── subscribe(listener) ──> EventBus
EventBus ── events ──> agent_loop
```

事件字段沿用 Python 命名：消息事件有 `message`；工具开始有 `tool_call_id/tool_name/args`；工具结束有 `result/is_error`；回合结束有 `message/tool_results`；运行结束的 `messages` 只含本次新增消息。事件定义见 [events.py](../../src/pi_code_agent/events.py)。

## 在自己的程序中订阅

```python
import asyncio
from pi_code_agent import EventBus, UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool

events = EventBus()
unsubscribe = events.subscribe(lambda event: print(event["type"]))
history = []

try:
    asyncio.run(
        agent_loop(
            history,
            model=DemoModel("add", {"a": 2, "b": 3}),
            tools=[add_tool()],
            prompts=[UserMessage("计算 2 + 3")],
            events=events,
        )
    )
finally:
    unsubscribe()
```

`messages` 是已经存在的历史，`prompts` 是本次新输入，两者明确分开，旧历史不会重播。仍可像 s03 一样先把用户消息放进历史、再调用循环；这样会继续处理历史，但不会补发那条用户消息的事件。SDK 仍原地追加并返回全部历史，区别于 `agent_end.messages` 的本次新增范围。

## 失败时会怎样

普通工具失败成为工具错误结果，模型仍可以修正。模型返回 `error/aborted` 时保存回答、关闭回合和运行，不执行其中的工具。HTTP 异常或模型返回坏数据时，新增无正文的失败消息并发送结束事件，原异常继续抛出；这不等于模型真的生成了一段失败文本。

单次原生取消会补齐未完成的工具结果、发送结束通知，再传播 `CancelledError`。已完成结果先保存，再通知观察者，防止界面失败导致结果丢失。`agent_end` 仅表示没有后续事件，判断成功还要看最终消息状态或调用异常；显式轮数保护也会在末事件之后抛错。

订阅器失败抛 `EventListenerError`，其后不保证事件完整；二次取消也可打断结束通知。订阅器可以拖慢运行，因此应只做必要工作，不能在其中等待当前这次循环结束。后台队列和完整运行控制后续实现，编号待定。

## 测试

```powershell
python -m pytest tests/test_s04_events.py tests/test_config_cli.py tests/test_boundaries.py
```

测试同时运行 SDK 与本章循环，验证实际事件顺序、消息范围、异步等待、取消订阅、观察副本隔离，以及失败和取消路径。前三章保留原有无事件循环，s04 快照与当前正式循环保持结构一致。

## 参考与差异

- 已对齐：Pi 的运行/回合/消息/工具事件顺序、调用 ID、顺序等待订阅器和末事件等待；参考 `packages/agent/src/types.ts`、`agent-loop.ts`、`agent.ts`，以及 `test/agent-loop.test.ts`、`test/agent.test.ts`。
- 扩展与调整：Python 用 `EventBus` 提供订阅，不提前加入有状态 Agent 外壳；接收字典事件和现有消息对象。每个订阅器得到独立副本，订阅表变化从下个事件生效，避免观察者改写工具参数或使当前分发过程不确定。Pi 直接遍历 Set 并传递事件引用。上述差异均在本章测试中验证。
- 暂缓：`message_update` 和文本流式在 s05；工具增量、并行调度、订阅器取消信号及有状态 Agent 生命周期编号待定。当前使用 Python 任务取消，保留异常传播，不能声称已经移植 Pi 的完整 Agent 外壳。

来源、修改范围及 MIT 声明见 [第三方归属](../../docs/third-party-notices.md)。
