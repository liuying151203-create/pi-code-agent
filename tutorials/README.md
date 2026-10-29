# 从零实现可扩展 Agent

新主线每章都交付可运行示例、正式接口和测试。先完成核心组件，再通过扩展实现具体功能；SDK 不单独开章。

教程代码按章节独立保存，不随 `src/` 升级变化。运行章节示例时使用章内组件；正式开发使用 `pi_code_agent`。验证环境和源码清单见 [snapshots.json](snapshots.json)。

1. [s01：异步核心循环](s01_agent_loop/README.md)：工具结果为什么要交回模型。
2. [s02：工具契约与参数校验](s02_tools/README.md)：定义和执行怎样保持一致。
3. [s03：真实模型接入与协议适配](s03_models/README.md)：换模型协议为什么不用重写循环。
4. [s04：运行事件与订阅](s04_events/README.md)：怎样观察运行进度，并等待订阅器完成。
5. [s05：文本流式响应](s05_streaming/README.md)：收到增量就显示，最终回答到齐后才执行工具。

6. [s06：会话与状态](s06_sessions/README.md)：谁持有历史，何时落盘，怎样恢复。

以下按调整后的章节顺序列出。s07 正在实现；扩展章节已顺延编号，但尚未接入压缩能力，逐章接续方式见 [上下文压缩设计](../docs/context-compaction-design.md)。

7. [s07：上下文压缩](s07_compaction/README.md)，分阶段学习手动、自动和溢出恢复（开发中）

8. s08：扩展加载与工具注册（[现有快照](s08_extensions/README.md)，待接续压缩）

9. s09：Hook 拦截与压缩扩展（[现有快照](s09_hooks/README.md)，待接续压缩）

10. s10：命令与参数（[现有快照](s10_commands/README.md)，待接续压缩）

11. s11：取消与消息队列（[现有快照](s11_queues/README.md)，待接续压缩）

12. s12：资源加载（[现有快照](s12_resources/README.md)，待接续压缩）

13. s13：动态工具与重载（[现有快照](s13_reload/README.md)，待接续压缩）

已完成范围和暂缓专题见 [章节规划](../docs/extension-foundation-plan.md)。旧版教程完整保存在 [archive/v1](../archive/README.md)。
