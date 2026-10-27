# s07：扩展加载与工具注册

增加一个工具不必修改核心循环。本章加载可信 Python 工厂 `setup(api)`，由工厂注册工具，再把有效工具组装进会话。加载失败的工厂不留下部分注册，也不会阻止后面的正常扩展。

## 运行

```powershell
python -m tutorials.s07_extensions.code --demo --demo-tool hello --no-session --no-extensions --extension tutorials/s07_extensions/example_extension.py --workspace example_project/mini_app --prompt "问好"
```

脚本模型实际调用 hello，工具返回“来自扩展的问候”。`--no-extensions` 只关闭自动发现，显式 `--extension/-e` 仍加载，与 Pi 一致。

自动发现顺序为：项目目录 `.pi-code-agent/extensions/`、全局 `$PI_CODE_AGENT_HOME/extensions/`、显式路径。全局根默认是用户目录的 `.pi-code-agent`。发现不递归，只识别根目录 py 和子目录 index.py，去重真实路径；包内可以使用相对导入。

项目自动加载需要 `--trust-project-extensions`。Python 工厂有宿主同等权限，显式路径表示信任执行；本章没有 Pi 的交互信任记忆机制。SDK 发现的相对路径按 workspace 解析，CLI 显式路径按启动目录解析。

## Pi 与本章怎样执行

参考 Pi `e4c75a732` 的 `extensions/loader.ts`、`runner.ts`、`types.ts`、`agent-session.ts::_refreshToolRegistry` 及发现/运行器测试。

```text
发现路径 → 独立模块 → setup(api) → 暂存注册
                    失败：清空并报告；成功：提交
有效工具 → AgentSession → 原有模型/工具循环
空闲后 close → 旧处理函数失效 → 清理所属模块
```

组装依赖：

```text
setup ── register_tool(Tool) ──> ExtensionRuntime
ExtensionRuntime ── extensions ──> AgentSession
Tool ── builtin_tools/tools ──> AgentSession
```

[extensions.py](extensions.py) 的 `ExtensionAPI` 负责一次工厂事务，`ExtensionRuntime` 负责有序扩展、来源、加载诊断和关闭。注册时声明仍与处理函数绑定，校验与工具执行继续使用前面章节的实现。

优先级保持 Pi 的具体行为：同一扩展内同名工具取最后一次；多个扩展同名取先加载者；扩展可以覆盖内置工具；SDK 显式 tools 最后覆盖。`tool_sources()` 返回扩展注册层的获胜来源，SDK 覆盖层由调用方明确提供。

## SDK

```python
import asyncio
from tutorials.s07_extensions.extensions import ExtensionRuntime
from tutorials.s07_extensions.session import AgentSession
from tutorials.s07_extensions.demo import DemoModel
from tutorials.s07_extensions.example_extension import setup


async def run():
    runtime = ExtensionRuntime()
    try:
        await runtime.register(setup, source="inline hello")
        session = AgentSession(model=DemoModel("hello"), extensions=runtime, stream=True)
        return await session.prompt("问好")
    finally:
        runtime.close()


assert asyncio.run(run())[-2].content == "来自扩展的问候"
```

文件加载使用 `await runtime.load(paths)`，普通错误在 diagnostics 中，取消继续传播。会话和模型客户端仍由宿主管理，关闭扩展不关闭共享模型。

## 测试与差异

```powershell
python -m pytest tests/test_s07_extensions.py tests/test_s06_sessions.py tests/test_tutorial_snapshots.py
```

- 已对齐：工厂提交/失败丢弃、路径去重、注册优先级、普通错误隔离、显式路径仍可在禁用发现时加载。工具确实进入会话和模型请求，不只是保存名称。
- 扩展与调整：Python 替代 TypeScript/jiti；入口显式编译，宿主通过 API 提供 Tool/ToolResult。项目执行信任用明确旗标表达，不冒充已实现 Pi 的信任保存。忽略下划线 Python 辅助入口文件。
- 暂缓：Hook 在 s08，命令和参数在 s09，取消/队列在 s10，资源在 s11，动态注册与重载在 s12。本章工厂结束后不再注册，不声称已经支持热更新。

来源与 MIT 声明见 [NOTICE](NOTICE.md)。
