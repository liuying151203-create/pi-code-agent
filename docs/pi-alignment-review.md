# Pi 对齐与规划复核

2026-10-28。检查本项目 `c9d5e32` 的正式源码、s01–s12 教程、测试与现有规划。Pi 仍以原先锁定的 `e4c75a73222ae2c72abb5f5314fa35ee8effc508` 为准；同级 Pi 工作区 HEAD 已是 `72363383f`，本次通过 `git show <锁定提交>:<路径>` 读取旧版本，没有混用新 HEAD 的行为。

结论：s01–s12 完成了选定的扩展基座，但不能据此称为完整对齐 Pi。原规划按章节列出组件，缺少跨章节的行为验收；部分实际差异被误写成“已对齐”，另外一些后续能力只有“编号待定”，没有依赖和可验收结果。本次只记录问题、修订推进顺序，不修改运行代码。

下文 Pi 的 `core/`、`extensions/` 和测试路径分别位于 `packages/coding-agent/src/core/`、该目录下的 `extensions/` 和 `packages/coding-agent/test/`；`ai/src/` 位于 `packages/ai/src/`。所有结论均指上述锁定版本。

## 已确认的当前问题

2026-10-28 实施进度：A01 的“本次输入”简单统计、A02 thinking 跨章兼容、A03 s04 失败历史过滤及模型依赖按需加载已修复。会话累计、查询、缓存和费用以及 A04–A06 仍未实现。下表保留复核时的描述，修复与回归结果见对应 bug 记录。

后续补充：原复核将上下文压缩只列为编号待定、排在扩展基座之后，不足以指导实现。现已完成 [压缩设计](context-compaction-design.md)：s07 接续 s06 并分阶段实现手动和自动压缩，原扩展章节顺延为 s08–s13，s09 的 Hook 章节接入压缩扩展；本文件原顺序是历史复核意见，当前以 roadmap 和压缩设计为准。旧章节尚未迁移。

以下问题应修订对应章节及正式产品，不另开一章包装旧功能。修复教学快照时只回补本章已经需要的行为，分别更新哈希；不能直接把最新产品覆盖到旧章。

| 编号 | 问题与实际影响 | Pi 的行为与源码依据 | 修订范围与验收 |
| --- | --- | --- | --- |
| A01 | 用量只打印最后一次请求；工具闭环前面的消耗漏算。所有 s03–s12 CLI 及正式 CLI 都有此写法。 | `core/usage-totals.ts::addUsageToTotals`、`agent-session.ts::getSessionStats` 按记录累计，区分会话消耗与当前上下文。 | s03 先做本次输入的输入/输出简单累计；后续章保留这个行为。s05 验证流式不重复计数，s06 增加会话累计，s09 增加查询。缓存、费用后续再加；缺少 usage 不能冒充零消耗。关联 V2-B021。 |
| A02 | s03 能接收 thinking，但正式包与 s04–s12 仍拒绝该内容块。学习下一章会重新遇到同一个错误。 | `ai/src/types.ts::ThinkingContent`、`api/anthropic-messages.ts` 保留 thinking、签名和 redacted 数据；工具后的请求继续回传。 | 正式源码与所有受影响章节按各自完整响应/流式路径修复；s06 起还需验证存储编解码、恢复后回传，s08 起验证 context Hook 接受。不开启额外推理配置，不要求显示推理。关联 V2-B020、V2-B022。 |
| A03 | s04 仍把 error/aborted 回答重放给模型，s03、s05 与产品已有过滤，跨章出现行为倒退。 | `ai/src/api/transform-messages.ts` 跳过失败回答，本地失败记录与请求投影分开。 | 回补 s04；验证失败回答留在历史，但两种协议都不发送它。不借此加入自动重试。关联 V2-B004、V2-B018、V2-B023。 |
| A04 | s09 README 声称同名命令跨扩展“首个优先”已对齐；实际第二个命令不可调用。 | `extensions/runner.ts::resolveRegisteredCommands` 为同名命令生成 `name:1`、`name:2`；同版本测试明确验证。工具和 flag 的首个优先不能直接套给命令。 | s09–s12 与产品修订命令解析；两个同名命令均可调用，help 使用相同解析结果，重载后顺序稳定。关联 V2-B024。 |
| A05 | 普通命令处理异常直接从 run_command 传播；RuntimeError 会绕过 CLI 现有异常列表，导致退出。没有命令诊断。 | `agent-session.ts::_tryExecuteExtensionCommand` 捕获普通处理异常，发送扩展错误并视为已处理，不把失败命令再交给模型。 | s09–s12 与产品明确命令错误事件/诊断、交互继续及一次性执行退出码；取消继续传播。不能把命令失败伪装为成功。关联 V2-B025。 |
| A06 | steer/follow_up 直接入队，绕过 input Hook；s11 起也不展开 Skill/模板、不拒绝扩展命令入队。prompt 的 source 又固定写成 sdk，CLI 与扩展来源无法区分。 | `agent-session.ts::_queueUserInput` 依次检查扩展命令、运行 input、展开资源、入队；`suite/agent-session-queue.test.ts` 验证直接排队也运行 input。 | s08 明确输入来源；s10 用户文本先经 Hook 再入队，s11 接资源展开。区分用户输入与已经构造好的 custom 消息，避免对 custom 重复展开/拦截。验证 handled 不入队、transform 生效、资源只展开一次、扩展命令被拒绝。关联 V2-B026。 |

还有一项工程契约未收口：s03 的 `--help/--demo` 已避免加载模型 SDK，但 s04–s12 和正式包仍在导入阶段加载两个 SDK。屏蔽 openai 后，正式包导入即失败。项目完整安装声明了这些依赖，因此不把它说成“装好完整环境也不能运行”；缺口是离线教学及注入客户端的依赖边界未统一。按 V2-B019 的边界核对后续入口，不强迫用户为了离线演示安装另一供应商 SDK。

## 原规划漏掉或没有安排清楚的能力

这些属于待设计/待实现能力，不等于当前已承诺功能全部有 Bug。下表给出实现依赖与验收；章节号尚未决定的明确标为“编号待定”，不能据此提前加入未使用接口。

| 主题 | 当前状态和遗漏 | Pi 参考 | 合理安排与验收 |
| --- | --- | --- | --- |
| 实际编程工具 | 正式 CLI 只组装 read；没有 write/edit/shell，无法完成“读代码→修改→运行验证”的编程任务。原规划先列审批/Todo 实践，缺少它们应服务的实际工具闭环。 | `core/tools/index.ts`、`write.ts`、`edit.ts`、`bash.ts`、`powershell.ts`、`grep.ts`、`find.ts`、`ls.ts`。 | 扩展基座修订后优先补通用工具，编号待定。按文件写入、精确编辑、进程执行逐项教学，不在一章堆完；搜索工具随后补。验证重名/重叠编辑拒绝、换行与 BOM、错误/超时/取消、输出限额与完整输出定位；只修改临时示例副本。 |
| 工具执行契约与进度 | Tool.execute 当前只收 arguments，结果只有文本/错误/terminate；没有工具调用 ID、运行上下文、进度回调与结构化 details。s04 有开始/结束事件，不代表运行中的工具输出可观察。 | `extensions/types.ts::ToolDefinition.execute`、`AgentToolUpdateCallback`、`tool_execution_update`、工具结果的 details。 | 随第一个确实需要长时输出的工具补齐，编号待定；Python 取消机制保留，但子进程必须实际结束并等待回收。验证进度不误记成多条最终结果，details 不自动进入模型正文。 |
| 手工 shell 与命令历史 | s09 的四种效果分离只是扩展命令机制，不是 shell 执行。缺少以前讨论过的 `!`/`!!`、退出码、取消/截断信息与执行记录。 | `agent-session.ts::executeBash/recordBashResult`、`core/messages.ts::BashExecutionMessage/convertToLlm`，README 的 Bash commands。 | 在进程执行工具可用后接入，编号待定。共用执行器；两种命令均可保存记录，但 `!!` 不进入模型请求；忙时延后入历史，维护工具调用/结果顺序，执行命令不默认触发模型回合。 |
| 系统提示与扩展边界 | 当前是简单基础字符串加上下文/Skill 列表。没有随启用工具更新的使用说明与约束；没有 before_agent_start。context Hook 不能完整替代这两个职责。 | `core/system-prompt.ts`、`extensions/types.ts::BeforeAgentStartEvent`、`agent-session.ts` 的提示与工具集合准备。 | 工具集落地时补提示组装；在扩展实践需要按次调整系统提示时补 Hook，编号待定。验证当前工具集合、说明与实际可调用集合一致，本次修改不污染下次运行。供应商 payload/header/response Hook 另随模型适配补，不宣称“全部 Hook 已实现”。 |
| 模型设置与能力信息 | 只有一次启动的 ModelConfig；缺少模型目录、上下文窗口/能力信息、会话内切换、推理级别与每条回答的供应商/模型来源。缓存/费用无可靠数据基础。 | `model-registry.ts`、`model-resolver.ts`、`model-runtime.ts`、`ai/src/types.ts::Model/AssistantMessage`、setModel/setThinkingLevel。 | 模型能力与配置专题，编号待定；SDK/CLI 同期交付。先覆盖当前两种协议所需能力，不一次复制所有供应商。验证不支持的选项有明确处理、切换后的历史仍可转换、借用客户端不被误关、回答的来源可追溯。OAuth/更多供应商作为后续适配。 |
| 会话元数据与格式演进 | s06 已明确只恢复消息，模型与设置重建；但后续没有安排模型/推理变化、名称/标签、文件版本迁移。当前严格字段检查会拒绝未来直接新增的字段。 | `session-manager.ts` 的 model_change、thinking_level_change、session_info、label 与 migrateSessionEntries。 | 会话专题，编号待定；在新增字段/条目前设计旧文件兼容，不让 s06 提前实现树。验收旧格式可识别、迁移策略明确、未知未来版本拒绝、运行设置恢复符合明确优先级。密钥/客户端/函数仍不落盘。 |
| 不完整工具历史的请求修复 | 当前能在协程取消时补工具结果，但进程被终止后可能只落盘了工具请求；恢复时未补齐请求投影。不能把“能读 JSONL”理解为所有断点均可继续。 | `ai/src/api/transform-messages.ts` 的 orphaned tool calls 第二遍扫描，补错误结果而不执行工具。 | 会话恢复专题，编号待定，优先于跨模型/分支切换。验证缺结果请求被明确补为“无结果”，绝不推断工具已成功或重新执行有副作用工具；保留真实日志。文件损坏修复与协议投影修复分别处理。 |
| 重试与运行结算 | max_retries 仅是 SDK 请求参数；不存在会话层可观察、可取消的重试。原规划只在“压缩与恢复”中顺带提重试。 | `agent-session.ts::_isRetryableError/_prepareRetry`、`agent-session-retry.test.ts`。 | 运行恢复专题，编号待定。区别网络重试与上下文溢出；验证退避/次数/取消、最终成功或失败、空闲屏障等待全部重试完成，已执行工具不被自动重跑；避免 SDK 与会话层次数无控制相乘。 |
| 上下文预算、压缩与分支 | 规划虽列了这些主题，却没列预算来源、触发时机、完整工具批次保留、摘要失败恢复及队列关系。用量累计不是上下文占用。 | `getContextUsage`、`core/compaction/`、`session-manager.ts::buildSessionContext`、树/分支测试与 session-stats 测试。 | 模型能力信息→会话上下文重建→压缩/树与分支，编号待定。保留全部日志，模型只看选定分支/摘要后的上下文；摘要生成用量计入总消耗；压缩后占用未知时不显示伪精确比例；失败/取消不丢队列。 |
| 扩展上下文与资源扩展 | 目前 Hook/命令共用动态代理 ctx；它会暴露后续增加的 Session 公共成员。没有 Pi 明确的普通上下文/命令上下文边界，也没有 resources_discover、共享事件总线等扩展路径。 | `extensions/types.ts::ExtensionContext/ExtensionCommandContext`、`resources_discover`、扩展间事件 API。 | 在 s08/s09/s12 复核时先列出已支持的明确成员，不把 Python 转发代理当成全部接口已对齐；随资源扩展实践补发现 Hook，编号待定。验证普通 Hook 不能误用仅命令可用的会话替换接口，重载后上下文失效继续有效。扩展仍是可信代码，接口边界不是权限沙箱。 |
| 交互与诊断闭环 | 队列只有 SDK 能在运行中投递，CLI 阻塞读取且 Ctrl+C 退出；UI 只有 notify，没有确认/输入对话。现有 diagnostics 多只在启动打印，运行期错误可能无人显示。 | interactive-mode、ExtensionUIContext、扩展错误监听和队列事件。 | 先提供满足实际扩展需要的异步输入、取消后继续、确认/输入与错误通知，再做完整 TUI，编号待定。审批实践依赖交互或宿主注入，不能只演示固定 block 就称为可交互审批；补全/渲染/状态栏/主题分阶段实现。 |
| 设置、信任、资源发布与其他适配 | 配置只有 .env/环境；项目扩展信任用显式旗标。ignore/worktree/资源包已有暂缓说明，但缺少阶段验收。多模态、更多协议、JSON/RPC 模式同样未完整安排。 | settings-manager、project-trust、resource-loader、package-manager，docs/settings.md、packages.md、rpc.md。 | 按实际需求分成后续专题，编号待定。配置优先级、坏文件不覆盖、项目代码加载时机、资源来源/冲突先明确；包安装、登录、主题等不混入眼前的统计修复。不能把 Python 包、Pi npm 包和 Pi 会话格式说成兼容。 |

教学文档也有维护遗漏：s01 的后台运行控制、s02 的扩展注册/Hook、s04 的队列仍写“编号待定”，实际上已分别在 s10、s07/s08、s10 实现。后续修订应更新这些章节指引，而不是改旧章源码以追赶最新产品。旧交接中的“下一次从 s06 开始”也已与当前 s03 学习进度不符，本次在 roadmap 和交接补充中更正。

验收不能只检查类、字段或注册表存在。每项都要对应 Pi 的一个实际场景，核对请求内容、事件顺序、历史保存和失败路径；跨章节继承的基础行为另做同类缺陷审计。现有快照隔离测试继续保留，但它只证明独立可运行，不证明与 Pi 行为一致。

## 应保留的差异与已安排的暂缓

- Pydantic 校验、asyncio/异步生成器、显式 SDK 配置与客户端所有权属于 Python 设计选择；保留行为验证，不强求 TypeScript 代码形状一致。
- read 的工作区/敏感路径边界、默认不继承 Harness 祖先约定是已说明的产品选择；无需为了“对齐”撤销。新增工具也应明确各自实际边界。
- 重载先验证候选再关闭旧运行时有明确回归依据，可以保留；不能把注册回滚说成外部副作用回滚。
- 顺序工具执行、单分支会话、文本输入/输出可以作为教学阶段边界。并行、多模态、树、压缩和完整 TUI 已有暂缓记录；需要补依赖与验收，不代表要立即塞进 s03。
- Pi README 明确不内置 MCP、子 Agent、plan mode、Todo 和审批弹窗。这些应作为扩展实践或本项目扩展能力安排，不能把“内置 MCP/任务系统”列为 Pi 核心对齐缺口。本项目仍不加入 Zoekt、代码图或仓库验收业务。

## 建议推进顺序

1. 修订当前教学段：s03 简单用量累计；对应后续章和正式产品保留这一行为。补 thinking、s04 失败历史与依赖加载边界的明确 Bug，不借修复增加未来能力。
2. 继续 s04/s05 教学，验证事件、流式、取消和最终用量；讲到 s06–s12 时同步修订命令冲突、错误处理及用户输入队列路径。已有错误“已对齐”声明先标为待修订。
3. 基座讲解完成后，按小阶段补实际工具与编程任务验证；需要工具进度、系统提示组装和 shell 历史时一起实现必要接口。
4. 进入会话与模型能力专题，按依赖补设置、模型切换、格式演进、历史修复、重试、上下文预算、树/分支与压缩。每一项都先定本章验收，再写代码。
5. 用审批/Todo/命令历史策略等扩展实践检验接口；交互能力随实际需要加入，之后再扩展完整 TUI、资源包、多模态、更多供应商、JSON/RPC 等。

这不是本轮实现授权，也不是一次性重写所有章节。后续仍边教学边实现；SDK 与必要 CLI 始终跟随功能，不再开 SDK 包装章节。

## 本次验证与限制

读取 Pi 锁定版本的源码及相关测试，未安装依赖、请求真实模型、执行工具 shell、修改业务示例或提交 Git。使用已有 Python 环境，只替换网络边界，做了以下离线复现：

- 同一 thinking 响应：s03 成功保留 Thinking/Text；s04–s12 与正式 Provider 均抛不支持 thinking 的 ProviderError。流式和存储路径另外做静态检查，尚未新增自动回归。
- 两次模型调用分别 Usage(11, 2)、Usage(7, 3)：实际 CLI 只打印输入 7、输出 3，应累计输入 18、输出 5。
- s04 的 error 工具回答仍投影为 user/assistant/user。
- steer/follow_up 加入两条文本后 continue_：input Hook 调用 0 次，模型收到未转换文本。
- 两个扩展注册 same：仅 /same 匹配，/same:1 和 /same:2 均不匹配，只有第一个处理函数执行。
- 命令处理函数抛 RuntimeError：异常向上传播，diagnostics 仍为空。
- 隔离子进程阻止 openai 导入：正式包导入抛 ModuleNotFoundError，未触发模型请求。

现有针对相关章节、CLI、独立快照及边界的测试仍通过：这些测试没有覆盖上述全部缺口，不能用绿色结果反证行为已对齐。完整命令与实际结果见本次交接补充；本次不声称穷尽 Pi 的全部供应商兼容分支或全部运行时缺陷。

核对的重点 Pi 测试包括 `extensions-runner.test.ts` 的同名命令后缀、`extensions-input-event.test.ts` 的 source/streamingBehavior、`suite/agent-session-queue.test.ts` 的直接排队输入 Hook、`agent-session-stats.test.ts`、`agent-session-retry.test.ts`、`suite/agent-session-bash-persistence.test.ts`、`default-tools-setting.test.ts` 和 `tool-system-prompt-contributions.test.ts`。这里是阅读证据，没有运行 Pi 的测试或安装其依赖。
