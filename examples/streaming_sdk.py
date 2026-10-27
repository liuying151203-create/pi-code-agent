"""s05：外部程序接收文本增量，实际执行加法并保存完整回答，无需密钥。"""

import asyncio

from pi_code_agent import AgentEvent, EventBus, UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool


# s05 新增：打印真实收到的 delta，展示输出与完整历史是两份不同用途的数据。
def observe(event: AgentEvent) -> None:
    """读取文本增量并显示分段；最终回答只用于核对，不重复打印正文。"""
    if event["type"] == "message_update":
        change = event["assistant_message_event"]
        if change["type"] == "text_delta":
            print(f"delta: {change['delta']!r}")


async def main() -> None:
    """组装脚本流、实际工具和观察者，运行闭环后读取最终回答。"""
    model = DemoModel("add", {"a": 2, "b": 3})
    tools = [add_tool()]
    events = EventBus()
    events.subscribe(observe)
    history = []
    await agent_loop(
        history,
        model=model,
        tools=tools,
        prompts=[UserMessage("2 加 3")],
        events=events,
        stream=True,
    )
    print(f"已保存最终回答：{history[-1]}")


if __name__ == "__main__":
    asyncio.run(main())
