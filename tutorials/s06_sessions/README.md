# s06：会话与状态

前五章由调用方保存历史。现在让 `AgentSession` 持有当前消息和运行状态，`SessionStore` 负责追加式 JSONL 记录，程序退出后可以恢复。流中的 pending 快照只供观察，完成消息才落盘。

## 运行

```powershell
# 默认保存到启动目录 .sessions/s06/<目标ID>/<随机ID>.jsonl
python -m tutorials.s06_sessions.code --demo --workspace example_project/mini_app --prompt "读取 README.md"
# 指定文件：不存在时创建，存在时恢复
python -m tutorials.s06_sessions.code --demo --session .sessions/lesson.jsonl --workspace example_project/mini_app
# 纯内存运行
python -m tutorials.s06_sessions.code --demo --no-session --workspace example_project/mini_app --prompt "读取 README.md"
```

本章目录保存所有依赖组件，不引用 src/ 或其他章节。正式 CLI 使用同样参数，正式 SDK 从 `pi_code_agent` 导入。

## Pi 的执行过程

参考 Pi `e4c75a732`（0.85.1）的 `packages/agent/src/agent.ts`、`packages/coding-agent/src/core/agent-session.ts` 和 `session-manager.ts`：运行层持有当前消息；会话层在消息完成事件中追加记录；重开文件后构建下一次请求的上下文。

```text
新输入 → AgentSession.prompt → 既有 agent_loop
          ↓ message_end
SessionStore.append_message → flush/fsync → 外部订阅器 → 工具执行
程序重启 → SessionStore.open → 恢复消息 → 下一次 prompt
```

组装关系如下，箭头表示提供依赖：

```text
Model/Tool ── model/tools ──> AgentSession
SessionStore ── store ──> AgentSession
ConsoleDisplay ── subscribe(listener) ──> AgentSession
```

[session.py](session.py) 的 `AgentSession` 管理历史与 busy 状态，拒绝并发 prompt，直到末事件订阅器完成才空闲；`messages` 返回副本。模型由调用方创建并关闭，会话不接管客户端所有权。[code.py](code.py) 保留 s05 的完整循环。

`SessionStore` 写头记录和带 id/parent_id 的消息条目。写入失败时停止后续追加；加载遇到损坏、版本不符、工作区不匹配或非线性链时直接报错，不覆盖、不静默跳过。当前格式是本项目 version=1，不直接导入 Pi 的 JSONL。

## SDK

```python
import asyncio
from tutorials.s06_sessions.demo import DemoModel, add_tool
from tutorials.s06_sessions.session import AgentSession

session = AgentSession(model=DemoModel("add", {"a": 2, "b": 3}), tools=[add_tool()], stream=True)
history = asyncio.run(session.prompt("2 加 3"))
assert history[-2].content == "5"
```

需要持久化时传入 `SessionStore.create(path, workspace)`；恢复时传入 `SessionStore.open(path, workspace)`。模型、工具和系统提示由调用方重新组装，不把密钥、客户端或函数写入文件。存储假设单写入者；长度检查可以发现已发生的外部追加，不提供多进程事务。

## 验证

```powershell
python -m pytest tests/test_s06_sessions.py tests/test_config_cli.py tests/test_tutorial_snapshots.py
```

覆盖往返恢复、持久化先于工具副作用、并发拒绝、观察者中断补存、写入失败不重试、损坏拒绝、工作区匹配和客户端所有权。

## 参考与差异

- 已对齐：运行状态与记录存储分工、完成消息事件落盘、条目 ID 与父引用、默认保存及纯内存选择、末事件等待。
- 扩展与调整：Python 使用单个 `AgentSession` 封装运行状态，不复制 Pi 的完整 Agent 外壳；格式为 Python version=1，失败保留并继续抛原异常。模型设置和工具由外部显式重建。未完成的工具结果在运行收口时补存，避免观察者失败丢失真实执行结果。
- 暂缓：扩展注册在 s07，Hook 在 s08，自定义扩展状态条目在 s09，取消与队列在 s10；会话树、分支、压缩和损坏恢复编号待定。

来源与 MIT 声明见 [NOTICE](NOTICE.md)。
