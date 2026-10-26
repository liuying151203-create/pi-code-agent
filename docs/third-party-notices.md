# 第三方来源与许可

参考 Pi 本地提交 `e4c75a732`，coding-agent 包版本 0.85.1。核心循环、工具参数校验顺序和消息转换流程按 Python 异步生态移植并重新实现。参考入口：`packages/agent/src/agent-loop.ts`、`packages/ai/src/types.ts`、`utils/validation.ts`、`api/anthropic-messages.ts`、`api/openai-completions.ts` 及相关测试。

保留顺序执行、独立工具结果角色、失败结束与截断调用拒绝；Python 使用 Pydantic 参数模型，当前只提供文本与客户端函数工具，未移植流式增量、并行调度或完整模型能力。SDK 返回追加后的全部历史，区别于 Pi 低层循环返回本次新增消息的方式。后续迁移按中文章节明确记录差异，不把本轮当作完整复刻。

s04 继续参考同一提交的 `packages/agent/src/types.ts`（AgentEvent）、`agent-loop.ts`（runAgentLoop/runLoop、顺序工具事件）和 `agent.ts`（subscribe/processEvents），以及 `packages/agent/test/agent-loop.test.ts`、`agent.test.ts`。Python 保留八种非增量事件和有序等待；字段改为 snake_case，通过 EventBus 而非有状态 Agent 提供订阅，逐订阅器复制观察数据，订阅变更从下个事件生效。HTTP 异常通知后继续抛出，原生任务取消取代尚未移植的 AbortSignal；这些区别在 s04 说明和测试中记录。

以下 Pi MIT 声明适用于相关复用与移植部分，不代表归档之外的全部原创内容另行更换许可。

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
