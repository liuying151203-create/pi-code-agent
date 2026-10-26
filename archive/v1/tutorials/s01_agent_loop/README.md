# s01：Agent 的核心循环

这一章实现一个能调用工具的 Agent。模型决定下一步做什么，程序执行工具，再把结果交还模型；模型给出最终回答后结束。

## 运行

先按根目录 [README](../../README.md) 配好 Python 环境和 `.env`。本章使用 Anthropic Messages 接口，需要 `ANTHROPIC_API_KEY` 和 `MODEL_ID`。

```powershell
python -m tutorials.s01_agent_loop.code
```

可以输入“用 echo 输出 hello”，观察一次工具调用，再输入“你好”，观察直接回答。输入 `q` 退出。本章的 bash 工具在 Windows 上实际使用 cmd.exe；它直接运行模型生成的命令，请选择练习项目，权限控制在 s03 讲解。

## 循环为什么能完成任务

一次普通模型请求只返回一个回答。Agent 遇到工具调用时还要继续：

```text
用户任务 → 模型 → 工具调用 → 执行工具 → 回传结果 → 再次请求模型
```

在 [code.py](code.py) 中，`agent_loop()` 每轮做四件事：

1. 把已有消息和工具定义交给 `create_message()`。
2. 把模型回答加入历史。回答里没有 `tool_use`，就返回。
3. 有工具调用时，把工具名和参数交给 `dispatch()`，逐个执行。
4. 用原来的调用 ID 生成 `tool_result`，加入历史，回到第一步。

调用 ID 把结果与请求对应起来。模型看到命令输出后，才能判断任务是否完成，或者继续选择工具。一轮回答中的多个工具按原顺序执行，整批结果一起回传。

`TOOLS` 只是告诉模型有哪些工具，不会执行命令。循环会检查工具名是否在本次目录中，再调用实际执行函数。未知工具、参数不是对象、命令失败都会作为带 `is_error` 的工具结果回传，让模型根据错误修正。模型自身失败或取消则停止；输出被截断时，其中的工具调用不会执行。

## 作为 SDK 调用

循环不读取 `.env`，也不自己创建模型客户端。`main()` 负责组装依赖并接收终端输入，外部程序通过正式包使用同样的调用方式：

```python
from pathlib import Path
from pi_code_agent import agent_loop
from pi_code_agent.local_tools import LocalTools

backend = LocalTools(Path.cwd(), mode="default")
history = [{"role": "user", "content": "用 echo 输出 hello"}]
agent_loop(
    history,
    create_message=requester,
    dispatch=backend.dispatch,
    system="你是编程助手，任务完成后停止。",
    tools=[tool for tool in backend.tools if tool["name"] == "bash"],
)
```

这里的 `requester` 由调用方提供，接收 `system/messages/tools/max_tokens`，返回含 `content` 和 `stop_reason` 的模型响应；本章 `main()` 使用 `ModelRequester` 组装它。`history` 会原地追加消息，也是返回值。SDK 本身不打印结果，界面由调用方负责。

依赖关系就是：

```text
模型请求函数 ──create_message──> agent_loop <──dispatch── 工具执行器
工具定义列表 ─────tools────────> agent_loop
```

教程保留完整循环便于阅读；正式 SDK 使用 `src/pi_code_agent/loop.py` 中的产品循环，不导入教程。Hooks、观察者和保存回调按后续章节需要加入；本章无需先学习或组装它们。

## 测试

在项目根目录执行，不需要 API Key：

```powershell
python -m pytest tests/test_s01_agent_loop.py
```

测试模拟“模型先调用工具、再回答”的过程，并检查结果确实回到下一次请求、未知或截断调用不会执行、显式空工具列表不会被默认工具替换。相同边界同时验证教程和正式 SDK。

## 参考与差异

参考本地 Pi `e4c75a732`（0.85.1）的 [agent-core README](../../../pi/packages/agent/README.md)、`packages/agent/src/agent-loop.ts` 和 `test/agent-loop.test.ts`。Pi 将循环、模型请求和工具实现分开，本章沿用这种分工。

已对齐工具名查找、调用 ID 对应、工具错误回传、失败停止和截断调用不执行。当前选用 Pi 支持的顺序执行方式，使用 Python/Anthropic 消息格式；没有声称对齐 Pi 默认的并行执行。完整 schema 校验待 s03 修订接入，事件流和流式模型请求分别在 s08/s09 展开，并行及运行中取消编号待定。工具输出预算在 s02 加入。来源与 MIT 许可见 [第三方归属](../../docs/third-party-notices.md)。
