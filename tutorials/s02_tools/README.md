# s02：工具契约与参数校验

第一章把工具结果交还模型。这一章解决执行前的问题：模型生成的参数可能缺字段、类型错误，甚至不是 JSON 对象。程序必须先校验，才能调用工具。

## 运行

```powershell
python -m tutorials.s02_tools.code
```

脚本模型请求加法工具，参数是字符串 `"2"` 和 `"3"`。参数模型将它们转换为整数，处理函数实际相加，再把结果 `5` 交回模型。这个例子不请求网络。

消息、工具、脚本模型及文件工具都保存在本目录；运行时不引用 s01 或 `src/`。来源标记说明哪些设计保持不变，后续修改不会自动传回本章。

## 定义和执行放在一起

`Tool` 将名称、说明、参数模型和执行函数绑定在同一个对象里：

```python
from pydantic import BaseModel
from tutorials.s02_tools.tools import Tool, ToolResult


class Inputs(BaseModel):
    a: int
    b: int


async def add(arguments):
    return ToolResult(str(arguments["a"] + arguments["b"]))


tool = Tool("add", "两个整数相加", add, Inputs)
```

`tool.definition()` 从参数模型生成 JSON Schema 给模型看；执行前用同一个参数模型校验真实参数。因此定义不会说“需要整数”，运行时却完全不检查。

```text
模型请求 → 查找 Tool → 复制参数 → Pydantic 校验/转换 → await execute → ToolResultMessage
```

参数校验或处理函数失败都会带着原调用 ID 返回错误，不执行错误参数对应的函数。处理函数应返回 `ToolResult`，失败可抛异常；特殊场景也能显式返回 `ToolResult(text, is_error=True)`。

## 一个实际文件工具

本章 [file_tools.py](file_tools.py) 提供当前用到的只读 `read` 工具：

```python
from pathlib import Path
from tutorials.s02_tools.file_tools import read_tool

tool = read_tool(Path("example_project/mini_app"))
```

将它传给本章的 `agent_loop(..., tools=[tool])` 即可，循环沿用 s01 的执行顺序。`path` 指定文件，`offset` 是从 1 开始的行号，`limit` 最多 2000 行。实际返回受 50 KiB 限制，超限给出下一次 offset。

```text
参数模型 ──parameters──> Tool <──execute── 异步处理函数
Tool ──tools──> agent_loop
```

工作区由工具实例绑定，越界、凭证和运行记录路径会被拒绝。这是本项目保留的文件边界，区别于 Pi 默认允许更广路径的 read；它不提供 OS 沙箱，也不会限制其他可信 Python 工具的任意行为。

## 测试

```powershell
python -m pytest tests/test_s02_tools.py
```

测试包含嵌套结构、必填字段、枚举、范围约束、参数转换、失败不执行、修正后继续，以及 UTF-8 输出预算。完整循环保留在 [code.py](code.py)，与 s01 的循环结构一致。

## 参考与差异

参考 Pi `e4c75a732` 的工具定义、`packages/ai/src/utils/validation.ts` 和 `packages/agent/test/agent-loop.test.ts`，解决“定义给模型看了，执行时是否真的按定义检查”的问题。来源与许可见 本章 [NOTICE](NOTICE.md)。

- 已对齐：声明与执行绑定，先校验副本再执行，失败带调用 ID 回传，不修改原始参数。
- 本项目调整：Python 使用 Pydantic 同时生成 schema 和执行校验，转换细则与 TypeBox 有差异；read 保留本项目的工作区边界。嵌套校验与文件边界在本章测试中验证。
- 暂缓：扩展注册、Hook 和并行调度编号待定，后续分别验证动态工具接入、拦截顺序与执行调度。
