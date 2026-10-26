"""s14 新增：一个扩展注册工具、前置 Hook 和事件观察者的最小示例。"""

from typing import Any

from tutorials.s11_runtime_observability.code import AgentEvent
from tutorials.s14_extensions.registry import ExtensionAPI


# s14 新增：把相关回调放进一个小组件，避免 register 中嵌套多层函数。
class ReadPreviewExtension:
    """为指定文件提供短预览，不直接读取宿主文件。

    输入：ExtensionAPI；输出：read_preview 工具结果及终端观察提示。
    流程：前置 Hook 检查预览范围 → execute 委托 read_file → 事件通知观察者。
    权限和工具截断复用已有实现；本类不执行 shell，也不负责会话保存。
    """

    def __init__(self, api: ExtensionAPI) -> None:
        """保存本次启动的工具委托入口，不持有文件或会话对象。"""
        self.api = api

    def check_preview(self, name: str, arguments: dict[str, Any]) -> str | None:
        """只检查本工具范围；超过 80 行时阻断，其他工具继续。

        输入：模型工具名和参数；输出：None 放行或 Error 文本阻断。
        流程：匹配工具 → 校验参数对象、起始行和行数；不读取文件。
        """
        if name != "read_preview":
            return None
        if not isinstance(arguments, dict):
            return "Error: 预览参数必须是对象"
        start = arguments.get("start_line", 1)
        limit = arguments.get("limit", 20)
        if type(start) is not int or start < 1:
            return "Error: start_line 必须是正整数"
        if type(limit) is not int or not 1 <= limit <= 80:
            return "Error: read_preview 的 limit 必须在 1 到 80 之间；更多内容请分段读取"
        return None

    def execute(self, arguments: dict[str, Any]) -> str:
        """将预览参数交给已有 read_file 权限与后端。

        输入：path、可选 start_line 和 limit；输出：原样的读取结果或权限拒绝。
        流程：校验参数 → 补齐默认值 → api.call_tool('read_file', ...)。
        80 行仅是示例工具职责，不改变基础 read_file 的预算。
        """
        reason = self.check_preview("read_preview", arguments)
        if reason is not None:
            return reason
        if set(arguments) - {"path", "start_line", "limit"}:
            raise ValueError("只接受 path、start_line 和 limit")
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path 必须是非空字符串")
        # s14 新增：真实读取仍走权限入口，不用 Path.read_text 绕过只读检查。
        return self.api.call_tool(
            "read_file",
            {
                "path": path,
                "start_line": arguments.get("start_line", 1),
                "limit": arguments.get("limit", 20),
            },
        )

    def observe_result(self, event: AgentEvent) -> None:
        """观察预览结果，只打印状态，不参与执行控制。

        输入：事件总线已经产生的 tool_result；输出：终端提示，无返回值。
        流程：匹配 read_preview → 根据 is_error 展示状态；不修改正文、不请求模型。
        """
        if event.data.get("name") == "read_preview":
            status = "失败或被阻断" if event.data["is_error"] else "完成"
            print(f"扩展  read_preview {status}。", flush=True)


def register(api: ExtensionAPI) -> None:
    """把示例的三项能力注册到当前运行时。

    输入：main 创建的 ExtensionAPI；输出：无。
    流程：创建组件 → 注册工具定义与 execute → 注册检查 Hook → 订阅结果事件。
    参考 Pi 的模块注册方式；本项目用 s04 Hook 控制执行，s11 Event 观察事实。
    """
    preview = ReadPreviewExtension(api)
    # s14 新增：工具目录告诉模型如何生成调用，handler 决定宿主如何分发。
    definition = {
        "name": "read_preview",
        "description": "预览指定 UTF-8 文件的一段，默认 20 行、最多 80 行；与 read_file 使用同一权限入口。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 80},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }
    api.register_tool(definition, preview.execute)
    # s14 新增：注册回调而非改循环，检查先于读取，观察发生在工具结果产生后。
    api.before_tool_call(preview.check_preview)
    api.on("tool_result", preview.observe_result)
