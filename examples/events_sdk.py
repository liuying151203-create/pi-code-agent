"""s04：外部程序订阅事件，实际计算加法，无需密钥。"""

import asyncio

from pi_code_agent import AgentEvent, EventBus, UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool


# s04 新增：观察函数只看运行过程；移除订阅不会改变工具执行和模型历史。
def observe(event: AgentEvent) -> None:
    """输入事件，打印类型；工具完成时显示实际结果，不修改事件。"""
    print(event["type"])
    if event["type"] == "tool_execution_end":
        print(f"  {event['tool_name']} = {event['result'].content}")


async def main() -> None:
    """组装脚本模型、真实工具和事件总线，订阅后运行，最后取消订阅。"""
    model = DemoModel("add", {"a": 2, "b": 3})
    tools = [add_tool()]
    events = EventBus()
    unsubscribe = events.subscribe(observe)
    history = []
    try:
        await agent_loop(
            history, model=model, tools=tools, prompts=[UserMessage("计算 2 + 3")], events=events
        )
    finally:
        unsubscribe()


if __name__ == "__main__":
    asyncio.run(main())
