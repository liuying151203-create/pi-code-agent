"""s13：离线串起工厂、命令、状态、Hook、模型工具和安全重载。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from pi_code_agent import AgentSession, ExtensionRuntime, ResourceLoader, SessionStore
from pi_code_agent.demo import DemoModel


def setup(api):
    """声明工具、命令和参数；命令只保存状态，工具结果明确交给模型。"""
    # s13 新增：使用当前宿主类型，重载时重新创建工厂闭包。
    api.register_flag("prefix", type="string", default="你好")

    async def greet(arguments):
        return api.ToolResult(api.get_flag("prefix") + "，Agent")

    def note(arguments, ctx):
        ctx.append_entry("note", {"text": arguments})
        ctx.ui.notify("笔记已保存，模型没有运行")

    api.register_tool(api.Tool("greet", "返回问候", greet))
    api.register_command("note", note, description="只保存笔记")
    api.on("tool_result", lambda event, ctx: {"content": event["content"] + "！"})


async def main():
    """基础路径 → 功能组件 → 会话组装 → 运行及恢复验证；不访问真实模型。"""
    # s13 新增：基础依赖使用独立业务项目，临时状态留在 .sessions。
    workspace = Path("example_project/mini_app").resolve(strict=True)
    temporary_root = Path(".sessions")
    temporary_root.mkdir(exist_ok=True)
    model = DemoModel("greet", {})

    # s13 新增：先完成工厂和资源，再把实际组件交给会话。
    extensions = ExtensionRuntime()
    await extensions.register(setup)
    extensions.parse_flags(["--prefix=你好"])
    resources = ResourceLoader(workspace, defaults=False).load()
    with TemporaryDirectory(prefix="sdk-", dir=temporary_root) as directory:
        path = Path(directory) / "session.jsonl"
        store = SessionStore.create(path, workspace)
        session = AgentSession(
            model=model, extensions=extensions, resources=resources, store=store, notify=print
        )
        try:
            await session.run_command("/note 先学习，再验收")
            history = await session.prompt("问好")
            print(history[-1])
            await session.reload()
            assert session.get_active_tools() == ["greet"]
        finally:
            await session.aclose()
        restored = SessionStore.open(path, workspace)
        assert restored.entries[0]["type"] == "custom"
        assert len(restored.messages) == len(history)
        print("状态与消息分离、工具 Hook、重载和恢复验证通过")


if __name__ == "__main__":
    asyncio.run(main())
