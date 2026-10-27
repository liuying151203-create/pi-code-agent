"""s07：无需修改循环，通过宿主 API 注册一个实际执行的工具。"""


# s07 新增：结果类型来自当前宿主，工厂可用于正式 SDK 或独立教程。
def setup(api):
    """接收扩展 API，注册 hello 工具；返回值不触发模型或自动发送消息。"""

    async def execute(arguments):
        return api.ToolResult("来自扩展的问候")

    api.register_tool(api.Tool("hello", "返回扩展问候语", execute))
