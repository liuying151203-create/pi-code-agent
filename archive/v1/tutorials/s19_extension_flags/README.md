# s19：启动时给扩展传参数

s18 的 `/todos` 用来查看清单。现在我们想在启动时指定清单的标题，还想选择是否显示进度提示：

```powershell
python -m tutorials.s19_extension_flags.code --todo-heading 学习清单 --no-todo-progress
```

沿用 s17 的模型配置运行后，输入 `/todos`，标题会变成“学习清单”。之后添加任务时，清单照常保存，但不会显示额外的“进度”提示。

`/todos` 是运行过程中执行的命令，`--todo-heading` 是启动时传入的配置。本章解决的就是扩展怎样声明和读取这种配置。

## 为什么不能一次解析完参数

主程序原本只认识 `--workspace`、`--extension` 等内置参数。`--todo-heading` 属于 Todo 扩展，主程序事先不知道它存在。

如果一开始就检查所有参数，合法的扩展参数也会被报成“未知参数”。所以要先知道加载哪些扩展，让它们登记自己的参数，然后才能完整检查命令行。

本章 `main()` 的顺序是：

1. **先认出扩展模块。** 用 `parse_known_args()` 读取 `--extension` 等内置选项，暂时留下还不认识的参数。教程默认加载 Todo 扩展。
2. **加载扩展，登记能力。** 创建 `ExtensionAPI`，调用扩展的 `register()`，收集它的参数、工具和命令。这一步还没有模型和会话。
3. **再完整解析。** 把扩展参数加入命令行解析器，再用 `parse_args()` 检查所有参数，保存用户传入的值。参数写错就在这里报错；`--help` 也在这里显示并退出。
4. **创建实际运行组件。** 创建模型和 Agent，通过 `bind_runtime()` 把真实会话、工具和操作接口交给刚才的 `ExtensionAPI`。
5. **通知扩展可以开始了。** `initialize_extensions()` 固定本次参数，发送一次 `session_start`，让 Todo 从会话恢复清单，然后进入交互。

这里解析了两遍命令行，但**扩展只加载一次**。同一个 `ExtensionAPI` 先收集声明，再接上实际运行组件。如果重新加载扩展，就可能重复注册命令和事件。

读代码时，先顺着本章 [code.py](code.py) 的 `main()` 看这五步，再看 [flags.py](../../src/pi_code_agent/flags.py) 怎样保存参数，最后看 [registry.py](../../src/pi_code_agent/registry.py) 的 `bind_runtime()` 怎样接上实际依赖。

## 扩展怎样使用参数

Todo 在 `register()` 中声明参数的名称、类型和默认值：

```python
api.register_flag(
    "todo-heading", type="string", default="任务", description="/todos 的展示标题"
)
api.register_flag(
    "todo-progress", type="boolean", default=True, description="显示已保存的 Todo 进度"
)
```

这些声明由 `FlagRegistry` 保存。没有传参数时，标题是“任务”，进度提示开启；用户显式传入的值优先于默认值。布尔参数可以用 `--no-todo-progress` 关闭，字符串参数通过 `--todo-heading 学习清单` 赋值。

执行 `/todos` 时，Todo 才读取 `get_flag("todo-heading")`；收到工具结果时，再读取 `get_flag("todo-progress")`，决定是否显示进度。具体看 [todo.py](../../src/pi_code_agent/extensions/todo.py) 的 `show_todos()` 和 `show_progress()`。

**不要在注册时读出参数并一直存着用。** 那时命令行的值还没有应用，读到的可能只是默认值。应当在启动通知或实际执行时读取。

这也解释了为什么要有 `session_start`：注册时还没有会话，无法恢复旧清单；等 Agent 绑定真实会话后，扩展才能在这个通知里恢复状态。如果初始化出错，当前实例就停止使用，避免带着恢复了一半的状态继续执行。

SDK 调用不需要解析命令行，在 `create_agent()` 中传入 `flag_values={"todo-heading": "学习清单", "todo-progress": False}` 即可。两种入口最后都从同一张参数表取值。

## 怎么测试

使用已有 Python 环境，在项目根目录运行：

```powershell
python -m pytest tests/test_s19_extension_flags.py
```

测试使用模拟模型，不需要 API Key。它会检查：标题确实改变、进度提示确实关闭、扩展只加载一次、旧清单能恢复，以及错误参数和初始化失败会被正确处理。全部通过时，终端会显示 `passed`。

也可以先查看实际参数帮助，这条命令不需要模型配置：

```powershell
python -m tutorials.s19_extension_flags.code --help
```

帮助中应该出现 `--todo-heading`、`--todo-progress` 和 `--no-todo-progress`。需要手动体验时，再使用本章开头的启动命令。

本章参考 Pi `e4c75a732`（0.85.1）的 `registerFlag()` 和 `getFlag()`：先登记参数，运行时再读取。对应源码在 `core/extensions/loader.ts` 和 `core/agent-session-services.ts`。我们用 argparse 完成解析，增加布尔关闭开关，并对重名参数报错；运行中修改参数留到生命周期章节（编号待定）。许可见 [第三方归属](../../docs/third-party-notices.md)。
