# 新主线交接

当前批量任务：用户已明确授权 s06–s12 按章提交、提交后复查并在有问题时追加修复提交。范围与验收见 extension-foundation-plan.md。s06 首轮完整验证 465 passed、Ruff 通过；实际命令为 `..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\s06-d701d4ad -p no:cacheprovider --tb=short`。随后提交实现并继续提交后复查；会话 JSONL 仅线性，默认保存与 --no-session 可选，客户端由创建者管理。旧章快照不改。

## 当前进度：s05 文本流式响应（2026-10-27）

本轮基线 `69d4a75`，Pi 仍锁定 `e4c75a732`（0.85.1）。新增参考 `agent-loop.ts::streamAssistantResponse`、`packages/ai/src/types.ts`、`api/anthropic-messages.ts`、`openai-completions.ts` 和 `transform-messages.ts`。Chat 字段同时核对官方流式文档，链接见 s05 README。

正式 SDK 保留 Model.complete，新增 StreamingModel.stream 和 AssistantStreamEvent；`agent_loop(..., stream=True)` 通过 aclosing 消费 start、文本事件及 done。历史中的 pending 回答始终只占一个位置，最终替换后才校验/执行工具。网络或协议失败、取消时保留真实内容和最新用量，流先关闭，原异常继续传播。失败回答不发回供应商，pending 历史在请求前拒绝。订阅器失败单独报告，不保证后续通知完整。

CLI 默认流式，ConsoleDisplay 及时输出 delta，并在最终消息到达时只补后缀；`--no-stream` 保留完整响应模式。兼容 Chat 服务可用 MODEL_STREAM_INCLUDE_USAGE=false 关闭 stream_options；额度字段沿用显式 token_parameter。不自动重试，不显示工具参数增量，不实现推理、多模态或并行调度。

s05 教学组件独立保存为 13 个 Python 文件，含本章完整循环、Provider、配置、CLI 和 example.py。清单只新增 s05 条目；原 s01–s04 的 39 个源码文件哈希全部一致，旧归档也未改。全部 52 个教学源码文件均接受独立快照校验。第三方许可随章保留，产品源码不导入教程。

最终 **344 passed**，Ruff 检查和格式检查通过；教学 SDK 示例、s05 流式/完整入口、正式 SDK 示例、正式 CLI 和帮助均实际运行。测试只替代网络边界，Anthropic 使用真实 SDK 流累积器；没有联网请求真实模型、安装依赖或执行提交。SDK 依赖下界按实际验证版本声明为 Anthropic 1.6.0、OpenAI 3.19.0，不声称早期 SDK 支持新流式字段。V2-B004、V2-B005 分别记录失败历史重放和首字前取消用量丢失的复现与修复。

本轮实际验证命令如下；再次运行 pytest 必须换成新的随机专用临时目录：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\s05-fefba462 -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m ruff check --no-cache src tests tutorials examples example_project
..\zero2pi\.venv\Scripts\python.exe -B -m ruff format --check --no-cache src tests tutorials examples example_project
$env:PYTHONPATH = 'src;.'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s05_streaming.example
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s05_streaming.code --demo --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s05_streaming.code --demo --no-stream --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B examples/streaming_sdk.py
..\zero2pi\.venv\Scripts\python.exe -B -m pi_code_agent --demo --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B -m pi_code_agent --help
```

下一步 s06 会话与状态，先阅读 Pi Agent 与 Session 的对应职责，再确定消息所有者和保存/加载的最小闭环；不要直接导入归档或提前加入完整会话树、压缩和恢复策略。

## s01–s04 教学快照隔离记录（2026-10-27）

用户确认旧章不能随公共产品组件升级而变化。本轮基线 `40e9100`，未新增章节。每章保存本阶段实际需要的组件，仅在本章内相对导入；不导入 pi_code_agent、归档或其他教程。正式包仍只使用 src/，也不依赖教程。

s01 是无参数工具闭环，只有 7 个 Python 文件，不包含 Pydantic 参数模型、真实 Provider 或用量字段；s02 为 8 个文件，加入完整参数校验和只读文件工具；s03 为 11 个文件，加入配置、两种 Provider 与无事件 CLI；s04 为 13 个文件，加入事件和本章冻结的 example.py。各章 NOTICE.md 保留 Pi 声明，引用版本仍是 `e4c75a732`（0.85.1）。正式 SDK 示例保留在 examples/；学习示例使用各章自己的类型，不混用产品类型。

测试不再拿产品消息去驱动教学循环，也不再要求旧章代码始终等于最新产品。行为测试按阶段加载真实组件，附加单章复制、进程隔离和双向导入阻断验证。`tutorials/snapshots.json` 保存 39 个 Python 文件的 UTF-8/LF 规范化哈希及验证环境：Python 3.12.10、Pydantic 2.13.5、Anthropic 1.6.0、OpenAI 3.19.0、python-dotenv 1.2.3。产品升级不刷新旧快照；旧章 Bug 修复要明确记录、回归并更新清单。外部库仍需匹配对应环境，快照不包含虚拟环境。

最终 **211 passed**，包括归档哈希与四章隔离运行。实际完整测试命令为：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\snap-c69477d0 -p no:cacheprovider --tb=short
```

再次运行必须换成尚不存在的随机专用临时目录。复用已有环境，未安装依赖、未请求真实模型、未执行提交。V2-B003 已记录根因和边界。后续进入 s05 时，在新章演进流式组件，同时更新 src/ 的正式能力；不得自动覆盖 s01–s04 的冻结源码。

## s04 实现记录（2026-10-27）

已完成 s04 运行事件与订阅。Pi 仍对齐 `e4c75a732`（0.85.1），新增参考 `packages/agent/src/types.ts`、`agent-loop.ts`、`agent.ts` 及对应测试。工作开始时本项目 HEAD 为 `9d2f925`；期间检测到用户新增提交 `9ec6770`，保留该提交继续完成剩余工作，代理未执行提交。

正式 SDK 新增 `AgentEvent`、`EventBus`、`EventListener`、`EventListenerError`；`agent_loop` 新增可选的 `prompts` 和 `events`。已有历史不重播，八种非增量事件按 Pi 顺序发送，异步订阅器按注册顺序等待，末事件的等待也属于运行完成条件。`agent_end.messages` 只含本次新增消息，函数返回仍为全部历史。观察副本隔离、订阅表变更从下个事件生效，是明确记录的 Python 调整。

终端默认通过订阅显示工具开始和完整消息，s03 入口明确使用无事件模式；s01–s03 保留各自循环，s04 快照与当前正式循环做结构一致性校验。新参数均可省略，旧 SDK 示例继续可用。HTTP/契约失败先追加失败消息并通知，再抛原异常；原生取消补齐未完成工具结果并传播。订阅器失败单独抛错，不承诺余下通知完整；二次取消也可以打断通知。工具结果在观察者处理前保存，避免丢失实际执行结果。

最终新主线 **106 passed**，Ruff 检查与格式检查通过；离线 SDK 示例、s04 入口、s03 入口及正式 CLI 均实际运行。测试包含冻结归档哈希核验，无须重复运行未修改的旧版测试。新增回归问题 V2-B002 已记录到 bug-log.md。没有安装依赖、初始化环境或请求真实模型。

实际测试命令如下；再次运行须换成尚不存在的随机专用临时目录：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp D:\dev\Agent\pi-code-agent\.sessions\s04-8a04fefc -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m ruff check --no-cache src tests tutorials examples example_project
..\zero2pi\.venv\Scripts\python.exe -B -m ruff format --check --no-cache src tests tutorials examples example_project
$env:PYTHONPATH = 'src;.'
..\zero2pi\.venv\Scripts\python.exe -B examples/events_sdk.py
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s04_events.code --demo --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s03_models.code --demo --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B -m pi_code_agent --demo --workspace example_project/mini_app --prompt '读取 README.md'
```

下一章 s05 仅增加供应商文本流和 `message_update`，让终端逐段展示，同时保留完整回答；不提前加入工具增量、并行调度、队列和有状态 Agent 外壳。

## 前三章重写记录（2026-10-26）

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
