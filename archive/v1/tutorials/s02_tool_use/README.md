# s02：工具定义与分发

s01 只有 bash。读取文件、定位代码和修改文本都靠命令行，会把许多本来可以由程序直接完成的操作交给模型拼命令。这一章增加专用文件工具，核心循环保持不变。

## 运行

沿用 s01 的环境和模型配置，在练习项目中运行：

```powershell
python -m tutorials.s02_tool_use.code
```

可以输入“读取 README.md”或“查找所有 Python 文件”，观察模型选择 `read_file` 或 `glob`。文件工具限制在工作目录内；bash 仍直接执行宿主命令，写入授权在 s03 讲解。

## 模型怎样找到工具

每个工具分为两部分：给模型看的定义，以及真正执行操作的 Python 函数。

`TOOLS` 中的名称、说明和参数格式随请求发送给模型。`TOOL_HANDLERS` 保存名称到函数的映射，例如 `read_file → run_read`。收到工具调用后，`dispatch_tool(name, arguments)` 查表并调用函数，返回结果文本。

```text
模型选择工具 → dispatch_tool → 对应函数 → tool_result → 模型
```

本章提供 bash、read_file、grep、write_file、edit_file 和 glob。新增工具时补充定义与处理函数即可，不修改循环。

```text
TOOLS ──tools──> agent_loop <──dispatch── dispatch_tool
TOOL_HANDLERS ──名称到函数的映射──────────> dispatch_tool
```

阅读 [code.py](code.py) 时，先看 `TOOLS` 和 `TOOL_HANDLERS`，再看 `dispatch_tool()`。`agent_loop()` 保留 s01 的执行顺序；`display_tool()` 只负责终端展示，SDK 可以使用不打印的分发器。

## 工具怎样返回可靠结果

读文件保留开头，shell 输出保留末尾，两者默认最多 2000 行或 50 KiB，先达到哪项就按哪项截取。读取被截断时给出下一次的 `start_line`；shell 超限时另存完整输出，并返回存档路径。非零退出码会报告失败，不能把错误输出当成成功。

编辑文件使用 Pi 的整批替换方式：

```json
{
  "path": "example.py",
  "edits": [
    {"oldText": "enabled = False", "newText": "enabled = True"}
  ]
}
```

每项替换都在原文件上定位，先精确匹配，必要时尝试规范化引号、空格等差异。匹配必须唯一，各项不能重叠；全部检查通过才写文件。因此第二项失败时，第一项也不会提前写进去。未编辑的行、BOM 和原来的换行格式会保留。

编辑算法在正式包 [editing.py](../../src/pi_code_agent/editing.py) 中，教程和 SDK 共用。旧的 `old_text/new_text` 单块调用仍可使用，模型看到的定义统一使用 `edits`。

## 作为 SDK 调用

外部程序使用绑定工作区的 `LocalTools`，不用修改全局工作目录，也不导入教程：

```python
from pathlib import Path
from pi_code_agent.local_tools import LocalTools

backend = LocalTools(Path.cwd())
text = backend.dispatch("read_file", {"path": "README.md"})
```

要交给模型选择工具，就像 s01 一样，把 `backend.tools` 和 `backend.dispatch` 传给正式包的 `agent_loop()`。`dispatch` 是执行接口，授权由调用方的权限链控制；上述例子只展示读取。本章的编辑修正也同步用于正式 Agent 的实例后端和原有函数式工具入口。

## 测试

在项目根目录运行，不需要 API Key：

```powershell
python -m pytest tests/test_s02_tool_use.py tests/test_s02_edit_alignment.py
```

测试检查工具分发顺序、读取和 shell 输出预算、编辑歧义拒绝、整批失败不写入，以及 BOM/CRLF 和未修改行的保留。编辑行为同时验证教程、正式工具函数和 SDK 实例后端。

## 参考与差异

参考本地 Pi `e4c75a732`（0.85.1）的 [coding-agent README](../../../pi/packages/coding-agent/README.md)、`core/tools/`、`packages/agent/src/harness/tools/edit-diff.ts` 及 `coding-agent/test/tools.test.ts`。沿用 Pi 先介绍用法、再解释行为的写法，正文用中文说明。

已对齐读取/命令输出预算，以及整批编辑的原文定位、唯一性、重叠检查和格式保留。Python 中继续使用 `read_file/write_file/edit_file` 等已有名称，避免改名破坏现有调用。grep 仍只对指定文件做字面搜索，完整目录搜索和正则在搜索工具修订时接入（编号待定）；读取图片留到多模态章节，编号待定。Pi 的完整参数 schema 校验待 s03 修订接入，编辑 diff 展示和并发写入队列编号待定。来源与 MIT 许可见 [第三方归属](../../docs/third-party-notices.md)。
