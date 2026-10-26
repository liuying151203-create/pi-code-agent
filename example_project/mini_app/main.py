"""供 Agent 阅读的独立示例，不依赖 Harness。"""


def greet(name: str) -> str:
    """根据传入名称返回问候语。"""
    return f"你好，{name}！"


if __name__ == "__main__":
    print(greet("学习者"))
