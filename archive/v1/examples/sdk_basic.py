"""s16：程序直接调用正式 SDK 的离线示例，不需要模型密钥或 Docker。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# 来自 s10/s15：保持；调用正式包的 Provider 协议和消息类型，不依赖教程。
from pi_code_agent import ModelResponse, ModelUsage, create_agent


# s16 新增：用明确的脚本响应演示 SDK 接线，不把离线输出当成模型推理结果。
class DemoProvider:
    """先请求读取演示文件，再把实际工具结果放入固定回答。

    输入：SDK 的模型上下文和文本回调；输出：统一 ModelResponse。
    流程：用户任务 → read_file 调用 → 工具正文 → 固定格式的最终回答。
    边界：只演示一次读取，不访问模型服务；示例未启用摘要。
    """

    model = "offline-sdk-demo"

    def stream(self, *, on_text, **kwargs: Any) -> ModelResponse:
        """读取最近一条上下文，返回工具调用或带真实读取正文的演示回答。"""
        content = kwargs["messages"][-1]["content"]
        if isinstance(content, str):
            return ModelResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "read-note",
                        "name": "read_file",
                        "input": {"path": "note.txt"},
                    }
                ],
                model=self.model,
                stop_reason="tool_use",
                usage=ModelUsage(),
            )
        text = "离线脚本收到实际文件内容：\n" + content[0]["content"]
        on_text(text)
        return ModelResponse(
            content=[{"type": "text", "text": text}],
            model=self.model,
            stop_reason="end_turn",
            usage=ModelUsage(),
        )

    def complete(self, **kwargs: Any) -> ModelResponse:
        """满足 Provider 接口；明确拒绝本示例未实现的摘要请求。"""
        raise RuntimeError("离线演示未启用摘要，请使用真实 Provider 演示压缩")


# s16 新增：调用方管理临时工作区、SDK 订阅与关闭，正式包不写入安装目录。
def main() -> None:
    """在临时项目中运行一次完整的正式 SDK 工具调用。

    输入：无；输出：实际文件读取正文、消息数和事件类型。
    流程：准备临时依赖 → 创建 Agent → 订阅 → run → finally 关闭 → 清理临时目录。
    边界：临时文件与记录退出即清理；零用量表示未报告，不伪造模型 Token 消费。
    """
    with TemporaryDirectory(prefix="s16-") as directory:
        # s16 新增：示例业务文件和 Agent 记录分别放置，运行不修改仓库原件。
        demo_root = Path(directory)
        workspace = demo_root / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("SDK 让程序复用同一套 Agent。\n", encoding="utf-8")
        data_dir = demo_root / "records"
        provider = DemoProvider()
        events = []

        agent = create_agent(provider=provider, workspace=workspace, data_dir=data_dir)
        try:
            agent.subscribe(events.append)
            messages = agent.run("读取 note.txt 并报告内容。")
            print(messages[-1]["content"][0]["text"])
            print(f"模型上下文消息数：{len(messages)}")
            print("事件：" + ", ".join(event.type for event in events))
        finally:
            agent.close()


if __name__ == "__main__":
    main()
