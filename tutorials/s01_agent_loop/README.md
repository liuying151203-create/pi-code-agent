# s01：异步核心循环

模型会提出“调用工具”，但需要程序替它执行，再把结果交回去。这一章把这个过程连起来：模型决定下一步，循环负责执行，模型不再请求工具时结束。

## 先运行一次

在配置好根目录 Python 环境后执行，不需要模型密钥：

```powershell
python -m tutorials.s01_agent_loop.code
```

脚本模型先请求 `project_name`，工具返回当前目录名，然后模型用实际结果回答。这里故意使用可重复的脚本模型，便于先看清闭环；第三章再接入真实模型。

## 循环怎样工作

[code.py](code.py) 保留完整的 `async agent_loop()`：

```text
用户消息 → await model.complete → 工具调用 → await execute_tool → 工具结果 → 再请求模型
```

用户输入、模型回答和工具结果分别是 `UserMessage`、`AssistantMessage` 和 `ToolResultMessage`。工具结果带着原调用 ID，因此模型知道它对应哪一次请求。这些内部消息不使用 Anthropic 或 OpenAI 的专属字段。

循环先保存模型回答，再看有没有 `ToolCall`。没有就返回；有则按顺序执行，把整批工具结果交给下一次模型请求。模型失败或取消时直接结束；回答因额度被截断时，其中的工具调用不会执行。

工具失败会成为 `is_error=True` 的结果，让模型选择修正。正文恰好以 `Error:` 开头也可以是成功，程序不再从文字猜状态。

## 程序里怎样调用

命令行和外部程序使用同一能力，正式包不导入教程：

```python
import asyncio
from pathlib import Path
from pi_code_agent import UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, project_name_tool

history = [UserMessage("当前项目叫什么？")]
model = DemoModel()
tool = project_name_tool(Path.cwd())
asyncio.run(agent_loop(history, model=model, tools=[tool]))
print(history[-1])
```

```text
DemoModel ──model──> agent_loop <──tools── Tool
消息列表 ──messages──> agent_loop
```

循环原地追加并返回同一份历史，不读取 `.env`、不打印、不保存文件。默认不限制轮数；显式传 `max_turns` 才限制。原生协程取消会向上传播，并补齐未完成的工具结果；完整的后台执行取消在后续运行控制章节展开，编号待定。

## 测试

```powershell
python -m pytest tests/test_s01_loop.py
```

测试同时验证正式接口和教程循环：结果进入下一次请求、顺序执行、错误与截断处理、取消传播以及显式轮数保护。

## 参考与差异

参考 Pi `e4c75a732`（0.85.1）的 `packages/agent/README.md`、`packages/agent/src/agent-loop.ts` 及对应测试，其核心解决“模型请求工具后怎样继续运行”。来源与许可见 [第三方归属](../../docs/third-party-notices.md)。

- 已对齐：独立工具结果、失败结束、截断调用拒绝执行；当前采用 Pi 支持的顺序执行方式，让执行顺序先看得见。
- 本项目调整：返回更新后的全部历史，便于调用方直接继续使用；Pi 低层接口返回本次新增消息。两者都需要调用方管理历史。
- 暂缓：事件在 s04 加入，流式在 s05 加入；并行调度与后台运行控制编号待定，在对应章节再验证调度和取消语义。
