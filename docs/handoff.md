# pi-code-agent 开发交接

交接日期：2026-10-22。本文记录交接时的状态，不代表已授权实施全部规划；新对话须重新检查工作区。

## 2026-10-23 实施更新：s18 修订与 s19 已完成

本节是最新实施状态，下方同日修订方向和 2026-10-22 快照保留作历史追溯。仍须检查实际 Git 与源码。

- s18 已提交为 `7a2f320`，s19 已提交为 `5c108e9`；继续工作时重新检查 Git。实现阶段没有安装依赖或创建环境，复用 `..\zero2pi\.venv\Scripts\python.exe` 进行离线验证。
- 用户最新要求：章节 README 要用通俗语言讲清设计，再给出简单测试方法；不堆接口清单、固定栏目或强制多张图。本轮据此重写 s18/s19 教程文字，运行时代码未改。
- s18：`handler(args, ctx)`，`ctx.ui.notify`、`append_entry`、`send_message`、`send_user_message` 分别控制通知、私有状态、模型消息和回合触发。CommandResult 只表示命令处理状态，CommandEntry 只审计，不自动进入模型上下文。`display` 与 `trigger_turn` 独立；命令内触发在 handler 和审计完成后执行同一个 agent_loop。
- `/help`、`/stats`、`/todos` 默认只通知用户。CustomEntry 保存 JSON 状态；CustomMessageEntry 仅投影 content，details 不发送模型。私有状态可在运行期间追加；模型忙时的消息投递仍明确拒绝，队列机制尚未实现。初版 command 失败记录仍可读作审计，不自动重发给模型。
- s19：FlagRegistry 提供 boolean/string 声明、默认值、覆盖和帮助；ExtensionAPI 可先无运行时声明，再 bind_runtime 一次。CLI 两遍解析但扩展 register 只执行一次，错误参数和扩展帮助在创建 Provider/Session 前处理。SDK 使用 `extensions=` 与 `flag_values=`；默认构造后注册的未知参数在首条任务保存前拒绝。
- `initialize_extensions` 校验并固定参数，发送一次 session_start，完成真实会话恢复。启动回调允许保存状态及发送消息，显式请求的模型回合在全部启动观察者完成后统一执行。初始化观察者异常或中断会关闭实例；完成初始化后的模型回合错误按任务失败传播，不重放初始化。
- Todo 的 `todo-heading`（string，默认“任务”）实际改变 `/todos` 标题；`todo-progress`（boolean，默认 True）实际控制已保存进度通知。CLI 用 `--todo-heading 学习清单 --no-todo-progress`；SDK 传同名 flag_values。状态仍从工具 details 恢复，展示配置使用本次启动值。
- 已修复并记录 B039：session_start 中断后不能让后续任务跳过未完成的初始化。回归修复前正式 SDK 与 s19 教程均实际失败为 DID NOT RAISE，修复后通过。
- 实际全量离线验证：`656 passed, 6 skipped`，使用全新 `.sessions/p19-ec007944` 及 `-p no:cacheprovider`。Ruff 检查、20 个变更文件的格式检查、两个实际入口的扩展帮助、相对链接、s19 与正式 Agent/工厂的结构核对及 Git 差异检查均通过。没有请求真实模型、运行真实 Docker 或构建 wheel。
- 教程入口：`tutorials/s18_commands/README.md`、`tutorials/s19_extension_flags/README.md`；SDK 说明 `docs/sdk.md`；Pi 来源与 MIT 声明在 `docs/third-party-notices.md`。下一章 s20 接入根 AGENTS.md，后续 s21 统一 Skills 来源、s22 提示词模板。
- 保留差异：同步 handler、文本消息、简单通知、共享参数命名；重名和选项冲突明确拒绝，boolean 增加反向关闭开关。异步 handler、忙时 Steer/Follow-up、完整 TUI、多模态、会话控制和热重载按后续对应章节落实，编号待定，不提前加入空接口。

## 2026-10-23 续接更新：Pi 优先与 s18 修订方向

本节记录原交接后的新增事实和用户最新要求；与下方 2026-10-22 快照冲突时，以本节及当前 AGENTS.md 为准。

- 用户明确要求：所有有 Pi 对应实现的功能都优先向 Pi 对齐，或在其基础上扩展、优化；允许合规直接复用、移植、改写代码，前提是讲清楚原实现及本项目改动，让用户理解关键调用和数据流。旧的“不要直接复制实现”约束已被这一要求替代，适用许可证、版权和许可声明仍须保留。
- lcc 继续负责渐进教学风格；Pi 负责设计与实现参照。每章先核对实际源码与测试，记录参考版本/路径，再说明已对齐、暂缓及扩展或优化。教学分阶段推进，不以最小闭环为理由取消当前已经需要的接口语义和选择权。
- 当前已提交 s16（`c9c459c`）与 s17（`6cb7857`）。工作区已有尚未提交的 s18 初版：命令注册表、`/help`、`/stats`、Todo 扩展 `/todos`、命令持久化及后续模型投影；正式源码与教程已同步。
- s18 初版结果策略固定，尚未对齐 Pi 的结果处理选择权：handler 无参数和上下文，强制返回展示文字与模型摘要，所有命令事实进入模型上下文，不能由命令主动触发模型回合。用户指出这一差异，下一步先修订 s18，再推进 s19 扩展启动参数。
- s18 修订应分开命令日志、界面通知、扩展状态、模型可见消息和回合触发，参考 Pi 的 `handler(args, ctx)`、`notify`、`appendEntry`、`sendMessage`、`sendUserMessage`。同步空闲状态下的展示选择、消息保存和回合触发先落地；运行期间的 Steer/Follow-up、取消和会话控制随对应章节实现，编号待定，不先放空接口。
- 最近核对的本地 Pi 为 `e4c75a732`，包版本 `0.85.1`。主要参考 `core/extensions/types.ts`、`loader.ts`、`core/agent-session.ts`、`session-manager.ts`、`messages.ts` 与 `examples/extensions/todo.ts`。这些是本地参考事实，不代表最新上游版本。根 LICENSE 为 MIT；实际复用时核对对应文件及第三方内容的适用声明。
- 上一阶段实际验证为全量离线测试 `617 passed, 6 skipped`，命令专项 `53 passed`，Ruff、格式、入口帮助、文档链接及 Git 差异检查通过；这些验证的是 s18 初版，不代表修订方向已经实现。本次规则更新没有修改运行时代码，也没有重新运行这些测试。

下方保留原交接快照用于追溯；“尚未实现 s16/s17”“下一步配置收口”等旧状态不再作为当前起点。后续仍先检查 Git、源码与当轮用户要求，不将整份路线图当作一次性实施任务。

## 项目与边界

- 当前项目：`D:\dev\Agent\pi-code-agent`，Python 编码 Agent、通用 Harness、SDK 与 CLI；非 Pi 官方项目。
- 业务项目：`D:\dev\Agent\pi-repo-agent`，通过正式包复用底座，实现仓库检索、理解、任务维护、修复与评测。
- 原项目：`D:\dev\Agent\zero2pi`，保留作迁移来源，不继续在这里实现新产品功能。
- 参考源码：`..\pi` 作为设计与实现的首要参照；`..\learn-claude-code`（简称 lcc）参考渐进教程与代码表达。代码复用遵循上方最新用户要求和当前 AGENTS.md，教程以中文讲清机制。
- 正式实现位于 `src/pi_code_agent/`；教程位于 `tutorials/`，保留 s01–s15（含 s13 A/B/C）。正式包不得导入教程，业务项目不得复制核心循环，也不得让底座反向依赖业务项目。
- Harness 是运行实现；SDK 是外部编程接口；CLI 是人工入口。它们是同一产品的不同层，不需要再创建第三个 SDK 项目。

## Git 与环境快照

- 交接核查时：`main`，HEAD `9bf995c`，95 条提交，工作区原本干净，未配置远程地址。
- 两个新项目已分别继承 zero2pi 的完整 94 条历史，再各追加一条拆分提交；不要重新初始化孤立历史或覆盖原项目。详见 `docs/migration.md`。
- 本次交接仅新增本文；没有提交、推送、安装依赖、调用模型或构建 Docker。
- 未迁移原 `.env`、虚拟环境、Session、Trace、索引与沙箱数据。核查时本项目没有 `.venv/Scripts/python.exe`；不要默认已经配置运行环境。
- 原项目已有解释器 `D:\dev\Agent\zero2pi\.venv\Scripts\python.exe`；是否复用先核查依赖和用户意愿，不自动创建环境或安装软件。

## 当前真正已有的能力

- `agent.py`：`Agent` / `create_agent` 组装 Provider、工具、会话、事件、统计、扩展与可选压缩。
- SDK 入口：`run(prompt)`、`subscribe(listener)`、`register_extension(register)`、`close()`；同步单任务，`run` 返回消息列表。
- `loop.py`：正式包唯一核心循环；请求模型、顺序执行工具、保存消息及结构化状态，再继续或结束。
- `models.py`：Anthropic / OpenAI Chat 兼容 Provider、归一化响应、流式输出与用量。模型 SDK 负责协议请求，不等于我们的 Agent SDK。
- `session.py` 等：线性 JSONL、上下文投影、压缩记录、会话级用量、工具 `details` 状态恢复；尚不是会话树。
- `registry.py` / `extension_base.py`：可信 Python 扩展注册工具、前后 Hook、事件处理器；Todo 是有状态扩展示例。
- `ToolResult.content` 交给模型；`details` 保存结构化状态，不直接当作模型正文。
- `local_tools.py`：默认只读；default 模式的宿主 shell/写操作仍逐次确认，不是 OS 沙箱。
- `sandbox_copy` / `sandbox_shared` / `sandbox_native`：已有 A/B/C 执行后端组件，但尚未统一接入当前 CLI 的后端选择。
- 外部产品可以使用 `create_agent`，也可以显式组装公开 `runtime` 组件并调用公开 `agent_loop`；不能把“可复用”误写成所有业务已统一走同一个工厂入口。

## 已核对的缺口

- CLI 当前只有 `--workspace`、`--data-dir`、`--session`、`--mode`、`--extension`；无统一 `/命令` 和扩展参数注册。
- 配置主要散落于环境变量；`.env.example` 有 `AGENT_MAX_ROUNDS`，但当前 `cli.py` 未将它接到 `Agent.max_rounds`。
- 存在 Skill 组件，但当前统一 Agent/CLI 尚未组装完整的 AGENTS、Skills、提示词模板资源加载流程。
- SDK 不自动读取 `.env`；传入压缩策略才启用自动压缩；CLI 自行加载环境并创建策略。
- 没有统一取消、运行中输入队列、运行期间切换会话、会话分支；`subscribe` 尚无取消订阅返回值，`close` 不负责终止正在运行的同步任务。
- 已有 Provider 重试配置，不等于已完成 Harness 级分类恢复、上下文溢出恢复、统一任务预算或工具安全重放。
- 扩展是宿主可信代码；工作区选择、只读模式、项目信任均不能自动构成扩展或 MCP 的 OS 隔离。

## 下一步任务与规划状态

上一轮只完成规划讨论，没有开始实现，也没有确定新章节编号。用户本次要求的是新对话交接，不是一次性实现整个路线图。

新对话先阅读 `AGENTS.md`、本文、`docs/sdk.md`、`docs/migration.md`，检查 Git 和实际实现，然后给出“配置与 SDK 接口收口”的最小设计，待用户确认后再实施。

第一阶段：统一现有配置与参数接线 → 命令及扩展参数注册 → AGENTS/Skills/模板加载 → 取消与生命周期 → 有限重试及上下文超限恢复。每次只落实一个可验证机制，同时验证 CLI 与 SDK。

第二阶段：运行期间新建/切换会话 → 会话树与分支 → Steer / Follow-up 输入队列。会话切换要重建依赖并解绑旧订阅；分支下的 Todo 等状态必须从当前分支恢复。

第三阶段：MCP 扩展 → 项目记忆扩展 → 单个只读子 Agent → 有预算的并行及工作流示例。

- MCP 接入现有工具调用、Hook、权限、事件及结果保存链，不另建核心循环；Server 进程和连接必须有明确生命周期。
- 长期记忆先做有来源、可查看修改的项目笔记；Session、压缩摘要、Todo 状态不等于长期记忆，不必立即再建向量数据库。
- 子 Agent 复用 SDK，使用独立上下文/会话和受限工具；限制层数、时间与用量，关联父子 Trace。独立实例或子进程不等于文件隔离。
- 工作流负责显式步骤与状态推进，Hook 负责约束，Event 负责通知；不要把可靠编排隐含在一组事件回调里。仓库定位、修改、测试、修复留在 pi-repo-agent。
- 会话分支与 Git 分支不同：切换对话不会自动恢复文件；Git/Worktree 作为扩展或仓库业务能力另行设计。
- 各项目教程独立编号；不要把原 zero2pi 的 s16 检索规划误当成新底座下一章。未定编号写“编号待定”，不提前创建空组件。

参考 Pi 时区分版本：本地参考仓库落后于上游；旧文档“不内置 MCP”的结论不能直接沿用，上次已核对上游将 MCP 作为内置扩展。实现前核对对应版本源码，不照搬最新版接口。

## 开发要求与验证

以项目 `AGENTS.md` 为准，尤其注意：

- 中文注释；核心类和上层方法说明职责、输入、输出、主要流程。上层逻辑显式展开，避免为缩短代码加入 `partial`、多层嵌套或意义不明的包装。
- 组装按“基础依赖 → 功能组件 → 运行时组装 → 交互入口”排列；教程保留完整循环。新增/修改块就近标具体章节、改动与原因，不给纯复用代码制造假差异。
- README 简洁易懂，流程图讲顺序，组装图讲依赖并标参数名；不能只有抽象概念或堆砌未实现规划。
- 用户强调先设计再开发，完成后两轮检查与必要优化，包括代码、测试、README 一致性；不要借机做无关大重构。
- 下载、安装、虚拟环境、配置和初始化，未经明确要求只给命令，不自行执行。默认不提交；每阶段给双语 Conventional Commits 提交命令。
- pytest 使用不存在的随机专用短 `--basetemp`，并加 `-p no:cacheprovider`。优先选系统临时目录下专用子目录，避免原仓库临时路径引发 Git 所有权问题；不可指向已有数据目录。
- Bug 写入 `docs/bug-log.md`，记录证据、原因、修复边界及回归。同类复发关联旧记录；提示词建议不能替代运行时保证。
- 历史验证（拆分阶段，并非本次重新执行）：底座 485 passed、6 skipped；业务项目 384 passed、4 deselected；规范检查和正式模块导入通过。此后 Git 迁移还做过底座 8 项、业务项目 30 项小范围回归。
- 尚未验证真实模型、真实 Docker 和 wheel 构建；不能把替身测试当作这些链路成功，也不能宣称检索效果已达到简历目标。

## 新对话的首条指令

请先读取 AGENTS.md 和 docs/handoff.md 完成交接，核对当前 Git 与实现，再给出“配置与 SDK 接口收口”的最小设计及验证范围。本轮先讨论，不安装依赖、不改代码、不提交。
