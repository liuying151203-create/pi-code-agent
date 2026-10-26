# s18：让用户直接执行 /命令

用户输入“帮我检查代码”时，需要模型理解任务。但输入 `/todos` 时，用户只是想看待办清单，程序直接读出清单就行了。

本章增加的就是这个入口：把 `/命令` 交给对应的 Python 函数处理。原有的模型循环不变。

## 一条命令怎样执行

以 `/todos` 为例，扩展加载时先登记命令：

```python
api.register_command("todos", todo.show_todos, description="查看当前会话的待办清单")
```

这行代码只是把名称 `todos` 和函数 `show_todos` 存进 `CommandRegistry`，此时不会执行函数。

等用户真正输入 `/todos`，程序才依次做这些事：

1. `main()` 发现输入以 `/` 开头，交给 `Agent.execute_command()`。
2. `execute_command()` 从命令表找到 `show_todos`，记下命令开始，然后调用它。
3. `show_todos(args, ctx)` 读取已经恢复的清单，通过 `ctx.ui.notify()` 显示给用户。
4. 程序记录命令成功，等待下一次输入。整个过程不用请求模型。

这里的 `args` 是命令名后面的文字，例如 `/note 先检查登录` 的参数是“先检查登录”；`ctx` 提供当前会话和显示通知的入口。`/help` 也读取这张命令表，所以新增命令后会自动出现在帮助里。

读代码时，先看本章 [code.py](code.py) 的 `main()` 和 `execute_command()`，再看 [commands.py](../../src/pi_code_agent/commands.py) 的命令表，最后看 [Todo 扩展](../../src/pi_code_agent/extensions/todo.py) 的 `show_todos()`，就能串起这条路径。

## 命令的结果给谁看

有时用户只想看一下状态，有时希望模型也知道这次操作。因此，命令函数自己选择结果的用途：

| 想做什么 | 使用哪个接口 |
|---|---|
| 在界面显示一句话 | `ctx.ui.notify("已保存")` |
| 保存扩展自己的数据，供以后恢复 | `api.append_entry("note", {"text": "先检查登录"})` |
| 留一条消息，让模型下次回答时能看到 | `api.send_message("先检查登录")` |
| 直接给模型一个新任务，让它继续工作 | `api.send_user_message("检查登录逻辑")` |

前两项不进入模型上下文。后两项才会成为模型看到的历史。

`send_message()` 还能分别控制“是否显示给用户”和“是否马上让模型工作”：

```python
api.send_message("先检查登录", display=False, trigger_turn=True)
```

这表示不在界面展示这条消息，但把它交给模型，并请求模型继续。只想留给下次任务时，把 `trigger_turn` 设为 False；它的默认值就是 False。

为了避免在命令执行到一半时启动模型，本项目先让命令函数执行完、记下成功，再进入原来的 `agent_loop`。如果命令函数报错，就记录失败并显示错误，不启动它请求的模型回合；此前已经保存的数据仍然保留。

命令日志只记录“执行过什么、是否成功”，用于事后查看。保存日志不等于发送给模型；模型需要知道的内容，应由命令明确发送。日志定义在 [command_entry.py](../../src/pi_code_agent/command_entry.py)，哪些记录交给模型则由 [会话处理](../../src/pi_code_agent/observable_session.py) 决定。

## 怎么测试

使用前面章节已经配置好的 Python 环境，在项目根目录运行：

```powershell
python -m pytest tests/test_s18_commands.py
```

测试使用模拟模型，不需要 API Key。它会检查：查看清单不会请求模型，明确发送的消息能被模型看到，要求继续工作时才启动模型，以及命令出错后不会误启动模型。全部通过时，终端会显示 `passed`。

如果想自己操作，沿用 s17 的模型配置启动教程：

```powershell
python -m tutorials.s18_commands.code
```

依次输入 `/help`、`/stats`、`/todos`，应该直接看到帮助、统计和清单；输入普通任务时会进入模型循环，输入 `q` 退出。

本章参考 Pi `e4c75a732`（0.85.1）：`core/extensions/loader.ts` 登记命令，`core/agent-session.ts` 查找并调用命令函数。我们保留这种设计，增加执行日志，并改为同步执行；模型忙时插入消息留到后续队列章节（编号待定）。许可见 [第三方归属](../../docs/third-party-notices.md)。
