# 新主线交接

2026-10-26，用户确认将旧内容归档，重新编写课程，先完成前三章。原基线是 `8a99dc7`。旧版 180 个受版本管理文件保存在 `archive/v1/`，哈希记录在 `archive/v1-manifest.json`；归档不包含密钥、环境和运行数据。

新主线当前是 0.2.0，位于 `src/pi_code_agent/`，公开异步 `agent_loop`、统一消息、Tool/ToolResult、参数模型、ModelConfig 及两种 Provider。当前 API 与旧同步版不同，旧 Agent、会话、Todo、沙箱等仍在归档，尚未迁入新实现；不得从新源码导入归档来假装已经接通。

前三章：s01 讲模型/工具闭环，s02 讲定义与校验绑定、结构化错误和只读工具，s03 讲 Anthropic/OpenAI Chat 适配及显式配置。每章保留完整异步循环，并通过结构和行为测试验证与正式实现一致。前两章默认使用离线脚本模型；第三章提供 demo 与真实模型入口。

Pi 参考版本锁定 `e4c75a732`（0.85.1）。采用其顺序执行方式、独立工具结果消息、工具失败回传和截断请求拒绝机制；Python 参数声明使用 Pydantic，转换细则与 TypeBox 不声称逐项相同。下一章 s04 事件，之后 s05 流式；完整路线见 roadmap.md。

本轮复用 `..\zero2pi\.venv\Scripts\python.exe`，未执行依赖安装、环境初始化或真实模型请求。归档迁移后独立测试为 711 passed、6 skipped；新主线为 66 passed。Ruff 检查和格式检查通过，三个章节入口、SDK 示例和 CLI 帮助均实际运行。12 份当前文档的 19 个本地链接与 6 段 Python 示例也已检查。

测试复现命令如下；再次运行须将临时目录名换成新的随机名称，不能复用已有目录：

```powershell
# 项目根目录，新主线
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\new-ec8bcc83 -p no:cacheprovider --tb=short
# archive/v1 中，使用归档的配置和测试
$env:PYTHONPATH = 'src;.;tests'
..\..\..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\old-c5082dc3 -p no:cacheprovider --tb=short
```

以下检查在项目根目录实际通过，入口运行时使用 `PYTHONPATH=src;.`：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m ruff check --no-cache src tests tutorials examples example_project
..\zero2pi\.venv\Scripts\python.exe -B -m ruff format --check --no-cache src tests tutorials examples example_project
$env:PYTHONPATH = 'src;.'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s01_agent_loop.code
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s02_tools.code
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s03_models.code --demo --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B examples/async_sdk.py
..\zero2pi\.venv\Scripts\python.exe -B -m pi_code_agent --help
git -c core.safecrlf=false diff --check
```

默认只测试新 tests/，Ruff 排除 archive/，包发现限定 src/。复用的项目解释器缺少 setuptools，未为此安装依赖；使用本机已有的 Codex Python 打包工具成功生成 wheel，再由项目解释器直接导入该 wheel 并运行加法 SDK 示例。包内没有归档或教程，包含 Pi 许可归属文件。归档通过目录内 .gitattributes 禁止换行转换，保证哈希在后续提交、检出后仍可核对。未执行 Git 提交。
