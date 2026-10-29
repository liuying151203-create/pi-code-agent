# 扩展基座章节

2026-10-27 的原编号 s06–s12 批次已实现并按章提交，范围经用户确认，不包含会话树、压缩恢复或完整 TUI。每章包含正式实现、SDK、CLI、独立教学快照及测试。当时用户授权实际提交；后续小阶段遵守当前规则，默认只给提交命令。现计划插入 s07 上下文压缩，原扩展章顺延为 s08–s13；下表保留当时原编号，迁移尚未执行，映射见 [压缩设计](context-compaction-design.md)。

2026-10-28 对照原 Pi 锁定版本复核，发现先前将同名命令优先级和用户排队输入笼统称为“已对齐”不准确。当前功能及规划缺口、源码证据、逐章验收见 [复核记录](pi-alignment-review.md)；这些问题尚未修复。下表是原批次的范围，不是完整 Pi 对齐清单。

| 章节 | 新机制与可验收结果 | Pi 参考 |
| --- | --- | --- |
| s06 会话与状态 | 消息所有者、JSONL 保存/恢复、并发拒绝与持久化屏障 | agent.ts、agent-session.ts、session-manager.ts |
| s07 扩展加载与注册 | 可信 Python 工厂加载、工具注册、来源及加载失败隔离 | extensions/loader.ts、runner.ts、types.ts |
| s08 Hook 拦截 | 输入和上下文处理、校验后的工具拦截、结果改写 | agent-loop.ts、extensions/runner.ts |
| s09 命令与参数 | 命令分发、旗标、UI 通知、扩展状态记录与模型消息分离 | extensions/types.ts、agent-session.ts |
| s10 取消与消息队列 | 活跃任务取消、等待空闲、steering/follow-up 顺序与投递选择 | agent.ts、agent-loop.ts |
| s11 资源加载 | 工作区上下文、Skills 与模板的发现和显式使用 | resource-loader.ts、skills.ts、prompt-templates.ts |
| s12 动态能力与重载 | 动态工具集合、扩展启停生命周期和可失败回滚的重载 | agent-session.ts、extensions/runner.ts |

Pi 参考版本保持 e4c75a732（0.85.1），每章先核对对应源码和测试。功能边界、接口语义、执行顺序和失败处理优先对齐；Python 或教学差异明确写在章内，不将仅有名称的注册表称为已对齐。

教学入口见 tutorials/README.md。实现完成不意味着跳过教学：后续仍逐章讲解并验收；会话树、压缩恢复和完整 TUI 另行排号。实际验证结果与提交见 handoff.md。
