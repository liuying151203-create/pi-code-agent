# 第三方代码归属

s18 的命令动作、扩展记录与模型投影，及 s19 的扩展参数机制，参考并按 Python 同步运行时移植 Pi 的设计和部分控制流程。参考仓库为 https://github.com/earendil-works/pi，本地提交 `e4c75a732`（包版本 0.85.1）。

对应源码：`core/extensions/types.ts`、`loader.ts`、`agent-session.ts`、`session-manager.ts`、`messages.ts`、`cli/args.ts`。本项目新增命令审计、Python 类型校验与同步调度边界；具体差异见各章 README。保留以下 Pi 许可声明，不代表本项目其余代码自动变更许可证。

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
