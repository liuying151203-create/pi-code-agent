"""通过正式包离线运行 SDK 示例，不导入教学或归档。"""

import asyncio

from pi_code_agent import UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool


async def main() -> None:
    """组装模型和工具，运行异步循环并打印最终回答。"""
    model = DemoModel("add", {"a": "2", "b": "3"})
    tool = add_tool()
    history = [UserMessage("2 加 3")]
    await agent_loop(history, model=model, tools=[tool])
    print(history[-1].content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
