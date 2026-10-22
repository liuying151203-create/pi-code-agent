# 通用 Agent 教程

在项目根目录安装正式包后运行，例如 `python -m tutorials.s01_agent_loop.code`。
章节是阶段快照，各自保留完整循环；正式包在 `src/pi_code_agent/`，不依赖本目录。

| 阶段 | 教程 |
|---|---|
| 核心循环与工具 | [s01](s01_agent_loop/README.md)、[s02](s02_tool_use/README.md) |
| 权限与 Hooks | [s03](s03_permission/README.md)、[s04](s04_hooks/README.md) |
| 会话与上下文 | [s05](s05_session_persistence/README.md)、[s06](s06_session_context/README.md)、[s07](s07_session_compaction/README.md) |
| 事件、流式、模型和统计 | [s08](s08_runtime_events/README.md)、[s09](s09_runtime_streaming/README.md)、[s10](s10_model_provider/README.md)、[s11](s11_runtime_observability/README.md) |
| 技能与沙箱 | [s12](s12_skill_loading/README.md)、[s13-A](s13_a_sandbox_copy/README.md)、[s13-B](s13_b_sandbox_workspace/README.md)、[s13-C](s13_c_sandbox_native/README.md) |
| 扩展与状态 | [s14](s14_extensions/README.md)、[s15](s15_stateful_extensions/README.md) |

正式接口见 [SDK](../docs/sdk.md)，镜像构建以 [沙箱说明](../docs/sandbox.md) 为准。本次只改模块/资源位置及必要的跨项目依赖，章节编号用于代码来源说明，不是 SDK 名称。
