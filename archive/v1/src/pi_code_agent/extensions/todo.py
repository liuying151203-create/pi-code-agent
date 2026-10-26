"""s15 新增：任务清单通过工具结果 details 保存，事件只显示已保存的进度。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from pi_code_agent.commands import CommandContext
from pi_code_agent.registry import ExtensionAPI
from pi_code_agent.result import ToolResult
from pi_code_agent.runtime import AgentEvent

# s15 新增：清单用于轻量进度记录，限制条数和文字长度，避免无限膨胀状态快照。
MAX_TODOS = 100
MAX_TEXT_CHARS = 500


# s15 新增：清单项是扩展自己的数据，不新增会话 Entry 或任务调度组件。
@dataclass(frozen=True)
class TodoItem:
    """描述一个清单项，不表示代码已经通过验证。

    输入：正整数 id、任务文字和完成标记；输出：可序列化清单项。
    流程：add 创建 → set_done 替换标记 → 快照写入工具结果 details。
    只使用完成/未完成两种状态，不增加任务依赖、调度或文件操作。
    """

    id: int
    text: str
    done: bool = False


# s15 新增：参考 Pi 的 todo 扩展，自有状态留在组件，持久化交给通用工具结果通道。
class TodoExtension:
    """管理小型清单，提供工具执行、人工查看和进度观察回调。

    输入：register 取得的可选快照、模型工具参数或人工 /todos 查询。
    输出：工具返回正文与状态快照；命令返回展示与上下文结果；事件显示已保存进度。
    流程：初始化恢复 → 工具更新或命令只读查看 → 对应执行入口保存 → 观察结果。
    边界：不直接写文件、不请求模型、不自动把 agent_end 当作任务完成。
    """

    def __init__(
        self,
        saved_details: dict[str, Any] | None = None,
        *,
        get_flag: Callable[[str], bool | str | None] | None = None,
    ) -> None:
        """创建空清单，或校验后恢复保存的状态。

        输入：已有 details 或 None；输出：本次启动独立的清单和下一个 ID。
        流程：设置初始值 → 若有快照则 restore_state，无效快照直接报告。
        """
        # s19 新增：查询展示配置的实际值，不在声明阶段缓存尚未应用的 CLI 覆盖。
        self.get_flag = get_flag
        self.todos: list[TodoItem] = []
        self.next_id = 1
        if saved_details is not None:
            self.restore_state(saved_details)

    def restore_state(self, details: dict[str, Any]) -> None:
        """从结构化快照恢复任务，不读取正文或摘要。

        输入：todos 数组和 next_id；输出：更新本组件状态。
        流程：校验整份快照 → 构造 TodoItem → 全部有效后才替换状态。
        边界：拒绝重复 ID、错误完成标记或超预算状态，不静默重置清单。
        """
        # s15 新增：整份状态先校验、后替换，损坏记录不能伪装成新的空清单。
        rows = details.get("todos")
        next_id = details.get("next_id")
        if not isinstance(rows, list) or len(rows) > MAX_TODOS:
            raise ValueError("Todo 快照 todos 必须是有界数组")
        restored = []
        ids: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "text", "done"}:
                raise ValueError("Todo 快照清单项必须包含 id、text、done")
            item_id, text, done = row["id"], row["text"], row["done"]
            if type(item_id) is not int or item_id < 1 or item_id in ids:
                raise ValueError("Todo 快照 ID 必须是互不重复的正整数")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise ValueError("Todo 快照任务文字无效")
            if type(done) is not bool:
                raise ValueError("Todo 快照 done 必须是布尔值")
            restored.append(TodoItem(item_id, text, done))
            ids.add(item_id)
        if type(next_id) is not int or next_id <= max(ids, default=0):
            raise ValueError("Todo 快照 next_id 必须大于已有 ID")
        self.todos = restored
        self.next_id = next_id

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """执行 list、add 或 set_done，并返回正文与完整快照。

        输入：action，以及该操作所需的 text 或 id/done。
        输出：ToolResult；参数无效时抛 ValueError，由注册层转换为工具错误。
        流程：检查操作与字段 → 校验参数 → 更新或读取清单 → 返回独立快照。
        set_done 显式赋值，重复调用不反转状态；只有明确调用才更新完成标记。
        """
        if not isinstance(arguments, dict):
            raise TypeError("todo 参数必须是对象")
        action = arguments.get("action")
        # s15 新增：每种操作的字段就地校验，错误参数不改变清单或生成成功状态。
        if action == "list":
            if set(arguments) != {"action"}:
                raise ValueError("list 只接受 action")
            done = sum(item.done for item in self.todos)
            lines = [f"任务：{done}/{len(self.todos)} 已完成"]
            lines.extend(
                f"[{'x' if item.done else ' '}] #{item.id} {item.text}" for item in self.todos
            )
            if not self.todos:
                lines.append("暂无任务")
            content = "\n".join(lines)
        elif action == "add":
            if set(arguments) != {"action", "text"}:
                raise ValueError("add 需要 action 和 text")
            text = arguments["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise ValueError(f"text 必须是 1 到 {MAX_TEXT_CHARS} 字符的非空任务说明")
            if len(self.todos) >= MAX_TODOS:
                raise ValueError(f"任务清单最多 {MAX_TODOS} 项")
            item = TodoItem(self.next_id, text.strip())
            self.todos.append(item)
            self.next_id += 1
            content = f"已添加 #{item.id}：{item.text}"
        elif action == "set_done":
            if set(arguments) != {"action", "id", "done"}:
                raise ValueError("set_done 需要 action、id 和 done")
            item_id, done = arguments["id"], arguments["done"]
            if type(item_id) is not int or item_id < 1 or type(done) is not bool:
                raise ValueError("id 必须是正整数，done 必须是布尔值")
            index = None
            for i, item in enumerate(self.todos):
                if item.id == item_id:
                    index = i
                    break
            if index is None:
                raise ValueError(f"未找到任务 #{item_id}")
            # s15 新增：相对 Pi toggle，显式 set_done 避免重复调用反转完成状态。
            self.todos[index] = replace(self.todos[index], done=done)
            status = "已完成" if done else "未完成"
            content = f"任务 #{item_id} 标记为{status}"
        else:
            raise ValueError("action 只支持 list、add、set_done")

        # s15 新增：快照属于这次结果；后续清单操作不能回写旧结果。
        details = {
            "action": action,
            "todos": [asdict(item) for item in self.todos],
            "next_id": self.next_id,
        }
        return ToolResult(content=content, details=details)

    def show_progress(self, event: AgentEvent) -> None:
        """在结果保存成功后显示进度，不改变任务状态。

        输入：携带 details 的 todo/tool_result 事件；输出：一行终端提示。
        流程：过滤其他工具、错误与 list 查询 → 从本事件快照计算完成数量 → 打印。
        不读取 self.todos，避免同批多次操作时把较早事件误显示为最终状态。
        """
        # s19 新增：启动布尔参数实际控制进度展示，不改变工具状态或模型正文。
        if self.get_flag is not None and self.get_flag("todo-progress") is False:
            return
        if event.data.get("name") != "todo" or event.data.get("is_error"):
            return
        details = event.data.get("details")
        if not details or details.get("action") == "list":
            return
        todos = details["todos"]
        done = sum(item["done"] for item in todos)
        print(f"进度  {done}/{len(todos)} 已完成", flush=True)

    # s18 新增：人工查看复用同一清单的 list 逻辑，不伪造模型工具调用或另存工具状态。
    def show_todos(self, args: str, ctx: CommandContext) -> None:
        """返回当前待办快照，供 /todos 展示与后续模型上下文使用。

        输入：空参数和命令上下文；输出：通知。
        流程：只读 list → 展示正文，不自动发送模型消息；对齐 Pi /todos 的人工查看语义。
        边界：命令记录不是 Todo 状态恢复来源，恢复仍使用 todo 工具的 details。
        """
        if args:
            raise ValueError("/todos 不接受参数")
        result = self.execute({"action": "list"})
        # s19 新增：字符串启动参数只改变人工查看标题。
        heading = self.get_flag("todo-heading") if self.get_flag is not None else "任务"
        content = result.content.replace("任务：", f"{heading}：", 1)
        ctx.ui.notify(content)

    # s19 新增：声明期还没有 Session，绑定并应用参数后从真实会话恢复。
    def restore_on_start(self, event: AgentEvent) -> None:
        """读取当前会话的 Todo 快照并恢复，不从命令正文或摘要猜测状态。"""
        saved = event.data["session"].latest_tool_details("todo")
        if saved is not None:
            self.restore_state(saved)


def register(api: ExtensionAPI) -> None:
    """声明展示参数、工具和 /todos，在运行时绑定后恢复清单。

    输入：声明期或已绑定的注册入口；输出：无。
    流程：声明参数 → 创建 TodoExtension → 注册工具与事件 → session_start 恢复状态。
    只读模式限制基础文件工具，Todo 更新的是会话元数据，不放宽文件写入权限。
    """
    # s15 新增：先从完整会话恢复，而不是要求模型重述历史进度。
    # s19 新增：先声明参数；get_flag 在运行时读到 CLI/SDK 的最终覆盖值。
    api.register_flag(
        "todo-heading", type="string", default="任务", description="/todos 的展示标题"
    )
    api.register_flag(
        "todo-progress", type="boolean", default=True, description="显示已保存的 Todo 进度"
    )
    # 来自 s18：保持；旧教学快照已经绑定会话，继续支持注册时恢复。
    saved_details = api.get_tool_details("todo") if api.is_bound else None
    todo = TodoExtension(saved_details, get_flag=api.get_flag)
    api.on("session_start", todo.restore_on_start)
    definition = {
        "name": "todo",
        "description": (
            "管理当前会话的任务清单：add 添加 text，list 查询最新进度，"
            "set_done 用 id 和布尔 done 明确更新完成状态。"
            "用户要求规划或查看清单时使用，不必为简单任务创建清单；完成标记不替代测试证据。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "set_done"]},
                "text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "id": {"type": "integer", "minimum": 1},
                "done": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
    api.register_tool(definition, todo.execute)
    # 来自 s15：保持；通过已有 Event 显示已保存进度，/todos 另走命令注册入口。
    api.on("tool_result", todo.show_progress)
    # s18 新增：仅注册此扩展时出现 /todos，和模型 todo 工具使用同一个组件。
    api.register_command("todos", todo.show_todos, description="查看当前会话的待办清单")
