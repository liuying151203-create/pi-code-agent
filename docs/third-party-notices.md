# 第三方来源与许可

参考 Pi 本地提交 `e4c75a732`，coding-agent 包版本 0.85.1。核心循环、工具参数校验顺序和消息转换流程按 Python 异步生态移植并重新实现。参考入口：`packages/agent/src/agent-loop.ts`、`packages/ai/src/types.ts`、`utils/validation.ts`、`api/anthropic-messages.ts`、`api/openai-completions.ts` 及相关测试。

保留顺序执行、独立工具结果角色、失败结束与截断调用拒绝；Python 使用 Pydantic 参数模型，当前只提供文本与客户端函数工具，未移植并行调度或完整模型能力；文本流式已在 s05 实现。SDK 返回追加后的全部历史，区别于 Pi 低层循环返回本次新增消息的方式。后续迁移按中文章节明确记录差异，不把本轮当作完整复刻。

s04 继续参考同一提交的 `packages/agent/src/types.ts`（AgentEvent）、`agent-loop.ts`（runAgentLoop/runLoop、顺序工具事件）和 `agent.ts`（subscribe/processEvents），以及 `packages/agent/test/agent-loop.test.ts`、`agent.test.ts`。Python 保留八种非增量事件和有序等待；字段改为 snake_case，通过 EventBus 而非有状态 Agent 提供订阅，逐订阅器复制观察数据，订阅变更从下个事件生效。HTTP 异常通知后继续抛出，原生任务取消取代尚未移植的 AbortSignal；这些区别在 s04 说明和测试中记录。

s01–s04 的教学组件已按阶段独立保存，不再直接导入 src/。各章 NOTICE.md 同时保留本段许可，复制单章目录时应一并携带。s01 仅保留实际使用的空参数契约，s02 加入 Pydantic；s03 加入模型与配置，s04 加入事件。教学隔离不代表新增 Pi 功能或改变正式 SDK 的行为。

s05 继续参考上述提交的 `agent-loop.ts::streamAssistantResponse`、`packages/ai/src/types.ts` 的文本事件，以及 `api/anthropic-messages.ts`、`openai-completions.ts`、`transform-messages.ts`。Python 使用异步生成器与 aclosing，保留完整响应接口，普通异常与原生取消携带最新快照继续传播；未实现工具参数增量展示、推理或多模态。原始工具 JSON 完整解析后才交给校验器，失败快照保留在本地但不重放。s01–s04 的快照保持原文，s05 单独保存源码和许可。

s06 参考同一版本的 `packages/agent/src/agent.ts`、`packages/coding-agent/src/core/agent-session.ts`、`session-manager.ts` 及会话相关测试。采用状态/存储分工、完成消息落盘、条目关联及默认持久化选择。Python SessionStore 使用自己的 version=1 线性文件，不直接导入 Pi 文件；树与分支尚未迁入。

s07 切点计算参考同一版本 `packages/coding-agent/src/core/compaction/compaction.ts::findCutPoint`；Python 按现有文本、工具调用和结果消息实现，尚未接入摘要、存储或自动压缩。

s08 参考 `packages/coding-agent/src/core/extensions/loader.ts` 的工厂事务与发现顺序、`runner.ts::getAllRegisteredTools` 的跨扩展首个优先，以及 `agent-session.ts::_refreshToolRegistry` 的内置/扩展/SDK 分层。Python 用独立模块命名空间加载可信工厂，不声称支持 npm 包或操作系统沙箱；动态注册留在 s13。

s09–s13 继续参考同一版本的 `extensions/runner.ts`、`loader.ts`、`agent-session.ts`、`session-manager.ts`、`agent.ts`、`agent-loop.ts`、`resource-loader.ts`、`skills.ts`、`prompt-templates.ts` 及对应测试。输入/上下文/工具 Hook、命令四种效果分离、双队列与代次失效按 Python 移植；模板参数解析和单次替换语义参照原实现改写。Python 使用 PyYAML safe_load、asyncio 原生取消和独立模块包；默认业务上下文范围更窄，重载先验证候选以保留失败前的可用代次。完整范围与差异见各章 README。第三方声明随每份独立快照保留。

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
