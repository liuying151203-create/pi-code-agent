"""s10：显式选择命令效果，不把返回值自动变成模型提示。"""


def setup(api):
    # s10 新增：执行 /note 参数时显示通知、保存状态，模型不自动运行。
    api.register_flag("label", type="string", default="笔记")

    def note(arguments, ctx):
        ctx.ui.notify(api.get_flag("label") + ": " + arguments)
        ctx.append_entry("note", {"text": arguments})

    api.register_command("note", note, description="保存一条笔记，模型不运行")
