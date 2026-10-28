# 新主线问题记录

## V2-B007：扩展缺少原生 ModuleSpec

s07 提交 d423a36 后复查，手工 ModuleType 缺少 __spec__/loader 等元数据；普通相对导入可以运行，但插件调用 find_spec 或 importlib.resources 时失败。`test_extension_has_native_module_spec_and_can_read_its_package_resources` 在产品/s07 均复现 __spec__ is None。修复用 spec_from_file_location(..., submodule_search_locations=...) 和 module_from_spec 构建完整包，再显式编译入口，保留新鲜源码与命名空间清理。回归核对 origin、包内资源和工具实际输出；不把此修复等同于 s12 的热重载。

## V2-B006：合法无末尾换行的 JSONL 续写后损坏

s06 提交 d78cae8 后复查，加载可以接受完整但无终止换行的末条 JSON，追加却未补行分隔符，导致后续读取 JSONDecodeError: Extra data。`test_valid_final_record_without_newline_can_be_appended_without_corruption` 在产品和 s06 两种运行时均复现。修复按 UTF-8 字节续写，必要时补一字节换行，再写新记录并同步；不修复无效 JSON，不改变单写入者边界。回归保留无换行中文消息并校验恢复后两条消息内容，见本轮交接记录。

## V2-B004：流式失败快照被再次发给供应商

2026-10-27，s05 首轮实现保留了网络中断前的真实文字，但旧协议投影仍把 error/aborted 回答当普通 assistant 消息重放。`test_retry_keeps_failed_snapshot_locally_without_replaying_it_to_provider` 在两种协议及 SDK/教程四种组合中复现失败：第二次请求角色实际为 user/assistant/user，预期仅 user/user。Pi `e4c75a732` 的 `packages/ai/src/api/transform-messages.ts` 明确跳过失败回答。

修复只在产品和 s05 的请求投影边界过滤 error/aborted，pending 则在开请求前拒绝；本地历史和失败文本不删除，不自动重试或修复任意孤立工具消息。s01–s04 冻结源码不改。回归覆盖失败后手动续跑、历史对象保留和 pending 拒绝，修复后的全量 344 项测试通过，实际命令见交接记录。

## V2-B005：首个文本片段前取消会丢失已收到的用量

2026-10-27，s05 最初只靠文本事件更新循环快照；供应商先返回输入用量、文字尚未到达时取消，失败回答的 usage 仍为 None。`test_cancellation_before_first_text_keeps_received_usage` 在 SDK/教程与两种协议的四种组合中均复现 AttributeError；实际已收到 input_tokens=11。

原因是供应商的最新元数据不一定产生文本事件。修复让 ProviderError 和原生 CancelledError 携带最新快照，循环在失败收口时优先使用它；异常类型及传播保持不变，流先关闭。回归核对早期取消的用量、aborted 状态及连接关闭，修复后的全量 344 项测试通过，实际命令见交接记录。

## V2-B003：教学循环快照仍依赖可变产品组件

2026-10-27，在 `40e9100` 基线中，四章的 `code.py` 仍从 `pi_code_agent.messages/tools/loop` 导入组件，s03、s04 的 main 还调用产品 CLI；原边界测试要求 s04 循环始终等于最新产品。直接证据是这些导入和跨版本比较：单独冻结循环不能阻止产品组件改变旧章行为，也无法把单章目录拿出来独立运行。

原因是把产品能力复用等同于运行时共享同一份可变源码。修复范围：每章保存实际使用的消息、工具、模型和入口组件，仅相对导入本章；s01 保留无参数契约，s02 加入 Pydantic，s03 加入配置/Provider/CLI，s04 加入事件。正式 CLI 移除仅为旧教程存在的兼容分支，正式 SDK 的当前行为保留。

回归按阶段构造各自类型，覆盖核心循环、参数校验、两种模型适配及事件语义；另将各章复制到临时目录，以隔离 Python 进程阻断产品和其他章节导入，四章入口均实际运行。反向阻断教程导入时正式 SDK 也能运行。全部 211 项通过，归档哈希仍一致。源码清单使用 UTF-8/LF 规范化校验，不再要求旧章始终等于最新产品。隔离冻结项目源码；外部库仍使用记录的对应环境版本。

旧版 B001–B041 完整保留在 [v1 问题记录](../archive/v1/docs/bug-log.md)。新主线使用 V2 前缀，后续定位问题时记录复现、原因、修复和实际验证。

当前首次重写的已验证行为包括：工具错误不依赖正文前缀；参数校验先于执行；截断调用不执行；原生取消传播并补齐未完成工具结果；注入客户端不被 Provider 提前关闭。没有据此宣称验证真实模型或实际网络兼容性。

## V2-B001：Chat 适配不应改写工具错误正文

2026-10-26，在本轮尚未交付的重写草稿中，Chat 适配器曾给 `is_error=True` 的工具结果加上错误前缀。静态对照 Pi `e4c75a732` 的 `packages/ai/src/api/openai-completions.ts` 可见，Pi 直接传递工具正文；这项添加既改变了工具输出，也没有协议依据。

修复仅限 Chat 消息投影：保留原文和调用 ID；内部 `is_error` 继续保留，Anthropic 继续使用其原生 `is_error` 字段。回归 `tests/test_s03_models.py::test_batch_results_keep_ids_and_only_protocol_adapter_wraps_errors` 分别检查两种协议的正文、调用关联与错误字段，新主线 66 项测试通过。

## V2-B002：坏模型返回值没有关闭事件回合

2026-10-27，s04 首轮实现中，自定义 Model 返回 `None` 或重复工具 ID 时，原有契约校验会正确抛错，但位于模型异常收口之外，观察者最后只收到 `turn_start`。复现命令：`python -m pytest tests/test_s04_events.py -k invalid_model_contract`，修复前正式循环和教程循环的四种组合均失败，末事件实际为 `turn_start`，而非预期的 `agent_end`。

原因是新事件边界没有覆盖已有响应校验。修复将类型和重复 ID 校验纳入模型调用的异常收口：追加无正文的失败消息，发送消息/回合/运行结束事件，继续抛出原异常，不执行工具。边界不变：订阅器自身失败和二次取消不承诺后续事件完整。回归测试为 `test_invalid_model_contract_closes_failure_events`；修复后四项回归及全部 106 项测试通过，实际命令见交接记录。

## V2-B008：关闭后 Hook 仍可执行

提交 e3fdaa1 后复查：保留 AgentSession，再 close 扩展运行时，prompt 仍触发旧 input Hook 并请求模型。两份实现的回归先失败。dispatch 增加关闭屏障，失败在任何 Hook 副作用之前，会话 busy 仍能复位；不改变正常工具注册行为。

## V2-B009：只投递消息期间会话被误认为空闲

8fab3ae 提交后复查，两份实现的 message_start 观察者挂起时 busy 为 false，允许新 prompt 插入。回归先失败；修复让消息投递占用会话到所有事件完成，失败时补存未完成通知的消息并复位 busy。后续 s10 复用这一屏障。

## V2-B010：末回合观察者投递的消息滞留

351fc71 后复查，外部 turn_end 观察者投递不触发模型的消息，内部投递订阅器已先执行，末回合没有下次机会。两份回归先失败；改为核心在所有 turn_end 观察者完成后显式等待 after_turn 屏障，再判断队列与结束。消息仍不插入工具批次中。

## V2-B011：嵌套引用文档被误识别为 Skill

252680e 后按 Pi skills.ts 的 includeRootFiles 核对，递归时错误接受子目录任意 Markdown。两份回归先失败；只在显式资源根接受松散文件，子目录仍递归寻找 SKILL.md，已有技能正文与引用读取行为不变。

## V2-B012：关闭期间可重入且取消会跳过模型清理

32f6c8d 后复查，shutdown Hook 等待时 busy=false，且关闭取消后 CLI 没有执行 model.aclose。两份测试先失败。启动/重载/关闭共用占用屏障；关闭后的投递和状态追加拒绝；CLI 用嵌套 finally 保证拥有的客户端清理。生命周期原生取消继续传播。

## V2-B013：重载改变文件与内联扩展覆盖顺序

32f6c8d 后复查，先注册的内联扩展原本覆盖后加载文件，reload 却把文件先加载导致同名工具来源改变。两份回归先失败。增加实际使用的有序来源重放；显式发现器提供新文件序列，按原文件槽位重放并追加新增入口，内联来源保持原位置。SDK 最高覆盖不变。

## V2-B014：轮数保护和输入接管导致消息丢失

最终跨章核对发现两条队列边界：达到 max_turns 后已经出队的 steering 没进入历史；input Hook handled 时没有进入循环，处理函数发出的不触发回合消息滞留。产品及 s10–s12 八项回归先失败。轮数保护退出前把已出队输入作为消息保存，仍抛 TurnLimitError；handled 返回前显式投递。仅回补这些 Bug 到 s10、s11，不引入后章组件，逐章更新冻结哈希。

V2-B014 补充：取消工具时同样没有正常 after_turn，四项回归证实不触发回合的消息滞留；finally 在工具结果补齐后投递，再进行持久化，原生取消仍传播。

## V2-B015：第一章演示模型把工具选择暴露成初始化配置

- 复现：基线 ed7a2ff 的 s01 DemoModel 接收 tool_name 和 arguments，但唯一入口只用 DemoModel()，演示也只有空参数的 project_name。模型的 complete 不分析用户问题或工具说明，构造参数却容易让读者误以为真实模型也需要在初始化时指定工具。
- 原因：为了复用脚本模型，提前加入第一章不需要的配置，模糊了模拟回答、模型选择和循环执行的职责。
- 修复：仅修订 s01 的 DemoModel，删除构造函数，在 complete 中明确返回预设 project_name 工具请求；统一模型接口和真实工具执行路径保留。README 直接说明模拟请求与真实模型接入点，更新该文件的冻结哈希；不刷新其他章节或正式包。
- 参考：Pi e4c75a732 的 packages/agent/test/agent-loop.test.ts 在模型替身内构造预设工具调用，执行函数仍由循环分发。保留这种职责划分；真实模型接入已在 s03 实现，不宣称脚本具备模型选择能力。
- 回归：实际 DemoModel 先生成请求而不执行工具，再经真实 agent_loop 得到绑定目录名称；未注册工具时收到真实失败结果并正常结束。另验证 s01 独立运行、冻结清单及既有循环行为。

## V2-B016：第二章沿用误导性的模型初始化配置

- 关联：V2-B015 同类问题。基线 4e0afd6 只修订 s01，没有核对 s02 的旧脚本入口。
- 复现：s02 仍以 DemoModel("add", {"a": "2", "b": "3"}) 指定请求；直接 DemoModel() 却请求未注册的 project_name。新增真实脚本回归先失败，观察到 project_name 与本章 add 不一致。
- 原因：将演示模型做成可配置工具调用器，并把第一章旧默认值带进第二章；初始化配置掩盖了本章应该展示的模型请求数据。
- 修复：仅修订 s02，移除构造函数，在 complete 中预设 add 的字符串参数，main 改为 DemoModel()。校验、参数转换和真实计算路径不变；更新 s02 demo.py/code.py 的冻结哈希，其他章节和正式包保持原样。
- 参考与验证：沿用 Pi e4c75a732 的模型替身构造请求、循环分发和校验后执行的职责；回归检查请求生成时不执行，处理函数实际收到整数 2、3，原始调用仍保留字符串，结果 5 回到下一次模型回答。验证第二章独立入口和既有参数校验行为。

## V2-B017：第三章离线入口沿用工具配置式模型构造

- 关联：V2-B015、V2-B016。第三章复核时发现 DemoModel 仍接受工具名与参数，默认请求 project_name，但离线入口只注册 read。
- 复现：直接 DemoModel() 经实际循环不能读到临时项目 README；新增回归先失败，工具名为 project_name 而非 read。
- 修复：仅调整 s03 DemoModel 和 CLI，构造改为无参，脚本回答预设 read/README.md；真实 Provider 继续由配置创建，工具仍单独注册。更新两份源码的冻结哈希。
- 验证：真实 read 工具读取临时项目文件，文件内容变化后下一次请求读到新内容；章节 CLI 与独立目录运行验证不加载模型密钥。

## V2-B018：第三章失败回答在后续请求中被重放

- 关联：V2-B004。该记录最初只修复产品与 s05，s03 原投影仍会发送 error/aborted 回答，复核本章时明确回补这一 Bug。
- 复现：历史包含带未完成工具调用的失败回答，再追加用户输入，两种协议发送的消息仍为 user/assistant/user。error、aborted 与两种协议的四项回归先失败；失败调用没有工具结果，可能破坏服务端的请求/结果对应关系。
- 原因与修复：请求投影没有区分完整回答和失败状态。按 Pi e4c75a732 的 packages/ai/src/api/transform-messages.ts 跳过失败回答，仅修改 s03 的两种消息转换器，本地历史保留，更新 providers.py 的冻结哈希。
- 边界与验证：不增加自动重试、流式或任意孤立消息修复，不删除失败记录。回归验证请求只含两次用户输入，原失败对象与内容仍留在历史；既有协议转换、调用 ID、异常传播与客户端所有权一并回归。

## V2-B019：第三章离线入口提前加载真实模型依赖

- 用户复现：运行 `python -m tutorials.s03_models.code --demo --workspace example_project/mini_app --prompt "读取 README.md"`，进入 cli 后导入 providers，因模块级 `from openai import AsyncOpenAI` 抛 ModuleNotFoundError。当前默认 Python 确认没有 openai，但已有 anthropic、pydantic 和 dotenv；此前代理使用另一个依赖齐全的虚拟环境，未覆盖此条件。
- 回归先失败：隔离子进程拦截 openai、anthropic、dotenv，--demo 和 --help 两项均在进入模式分支前失败；首先暴露 config 的模块级 dotenv 导入，随后同样会触发两个 SDK 的导入。
- 原因与修复：把运行分支依赖提前放到模块导入阶段。仅修订 s03：dotenv 移到 load_model_config；模型 SDK 移到各自 Provider 创建自有客户端的分支，注入客户端不导入 SDK；缺少真实 SDK 时以 ProviderError 说明缺少的依赖。保持完整响应接口、客户端所有权与请求转换不变，更新 config.py/providers.py 的冻结哈希。
- 差异依据：这是 Python 模块加载的调整，不新增 Agent 机制；离线只保留第二章已用的 Pydantic 依赖，不将项目声明的完整安装依赖说成不存在。
- 验证：离线与帮助入口在三个模型依赖均被禁止导入时仍能运行，真实 Provider 构造与关闭行为回归；用用户当前缺少 openai 的默认 Python 重跑原始命令，全程不安装依赖、不请求模型网络。
- 2026-10-28 跨章修复：s04–s12 和正式包同样将两个 SDK、dotenv 的导入移到实际使用处，注入的客户端不触发 SDK 导入。隔离子进程对正式包及 s03–s12 分别验证 help/demo、单供应商构造以及注入客户端，共 55 项通过；用户默认 Python 实际运行 s04 demo 和正式包 help 均通过。

## V2-B020：第三章拒绝模型返回的 thinking 块

- 用户复现：真实 Anthropic 模式输入“你好”，服务已返回内容，但解析器只接受 text/tool_use，遇到 thinking 就抛 ProviderError，正常文字也没有显示。
- 回归证据：CLI 入口使用真实 SDK ThinkingBlock 构造响应，复现相同错误；签名工具回传、脱敏块、无签名回退和 Chat 投影共五项回归先失败。
- 原因：将暂缓主动推理配置等同于可以拒绝模型已返回的推理内容；简单跳过也会丢失工具调用后请求所需的签名。Pi e4c75a732 的 anthropic-messages.ts 保留 ThinkingContent、签名和 redacted 标记，官方文档同样要求工具对话中保留这些块。
- 修复：仅修订 s03，增加实际使用的 Thinking 内容类型；接收 thinking/redacted_thinking，Anthropic 请求保留签名、脱敏数据及原顺序，空文字的签名块也回传；无签名时沿用 Pi 文本回退。Chat 投影只取文本/工具，避免把 Thinking 当工具或产生空消息；CLI 继续只打印 Text。更新 messages.py/providers.py 的冻结哈希，不改核心循环或其他章节。
- 边界与验证：不主动开启推理、不增加预算选项或推理显示，不把 Anthropic 签名用于其他服务。回归覆盖普通问候、工具执行、多轮回传、签名/脱敏数据保留和不输出推理；不请求用户真实模型或声称已经验证其服务端配置。

## V2-B021：CLI 把最后一次模型请求用量当成本次输入用量

- 2026-10-28 规划复核，基线 c9d5e32。离线替换 s03 网络模型，经真实循环与 read 工具运行两次请求，usage 分别为 (11, 2)、(7, 3)，CLI 最后只打印输入 7、输出 3，而本次应累计输入 18、输出 5。s03–s12 与正式 CLI 均只读取最后一条回答的 usage。
- 原因：模型返回字段与 CLI 展示都有了，但没有定义一次输入和一次模型请求的统计范围；测试未覆盖多请求汇总。
- 2026-10-28 修复：s03、正式 CLI 与后续各章仅遍历本次新增回答，累计已返回的输入/输出用量；缺失数据标明不完整，零用量与未知区分，demo 不编造统计。不新增统计管理器，不修改核心循环或 SDK 返回值。会话累计/查询仍留给 s06/s09 后续修订。
- 回归：真实 CLI 连续两次输入、实际 read 工具、完整/脚本流式回答，共 80 项通过，s03/s04 无流式的 8 项跳过；覆盖旧历史不串算、流式快照不重复、部分/全部缺失和有效零值。另核对离线 demo 不显示用量。网络异常继续使用原有错误路径，不从未返回数据推断消耗。

## V2-B022：thinking 兼容修复未覆盖后续章节与正式包

- 关联 V2-B020。2026-10-28 对 s03–s12 与正式 Provider 注入同一个包含 thinking/text 的完整响应：s03 成功，s04–s12 与正式包均抛“当前不支持 Anthropic 内容块：thinking”。没有请求网络。
- 原因：此前只修当前教学章，没有列出同类问题在正式包及其他快照中的受影响范围。流式解析、消息类型、持久化编码与 context 校验也尚不支持这些块。
- 2026-10-28 修复：s04–s12 及正式包保存 thinking、签名和 redacted 数据；s05 起流式快照保留这些块，s06 起会话编解码支持它们，s08 起 context Hook 可以传递它们。保留 s03 的无签名文本回退，Chat 请求不发送 Anthropic 签名。主动推理预算/展示不属于此修复。
- 回归：用真实 Anthropic SDK 流累积器及模拟网络验证工具往返、空文字签名、脱敏块、错误/取消；验证会话保存恢复、原签名回传、损坏块拒绝和 context Hook，共 65 项通过。完整响应、两种协议投影与 CLI 不显示推理也覆盖所有受影响阶段。旧版纯文本/工具会话兼容；包含新增 thinking 块的会话不能被旧程序解析。

## V2-B023：s04 仍重放失败回答

- 关联 V2-B004、V2-B018。2026-10-28 构造 user、error assistant（含未完成工具调用）、下一条 user，经 s04 _anthropic_messages 转换，仍得到 user/assistant/user；代码核对 Chat 转换也未过滤。s03、s05 及正式包已有过滤。
- 原因：旧章 Bug 修订没有同时审计同类快照，造成学习下一章时已有修复倒退。
- 2026-10-28 修复：只回补 s04 的两种协议请求过滤，本地保留 error/aborted 对象，不增加重试或任意历史修复。回归对正式包及 s03–s12 验证两种协议与两种失败状态，检查失败对象仍在原历史中。

## V2-B024：把工具的跨扩展优先级错误套用于命令

- 2026-10-28 两个内联扩展分别注册 same，经正式 AgentSession 实际调用：命令表只有 same，/same 只运行第一项，/same:1 和 /same:2 均未匹配。Pi 锁定版本 runner.ts::resolveRegisteredCommands 及 extensions-runner.test.ts 明确保留全部同名命令并加数字后缀。
- 原因：命令表复用了工具/flag 的首个优先策略，s09 README 又误标为已对齐。
- 状态：已定位，未修复；README 已标注待修订。范围为产品及 s09–s12 的解析、help、重载后稳定命名；工具与 flag 的既有优先级不因本问题改变。后续需增加两项命令均可调用的回归。

## V2-B025：扩展命令普通异常绕过诊断并退出 CLI

- 2026-10-28 注册抛 RuntimeError 的 /bad，经正式 session.run_command 调用，异常直接传播，runtime.diagnostics 为空；CLI 的异常捕获列表不包含 RuntimeError。Pi _tryExecuteExtensionCommand 则捕获普通命令错误并发扩展错误通知，仍返回已处理。
- 原因：普通 Hook 已有错误分流，但命令调用绕开了相同层面的诊断设计；规划没有命令失败的验收条件。
- 状态：已定位，未修复。s09–s12 与产品应定义命令诊断、交互继续及一次性调用失败状态，取消继续传播，不误触发模型、不冒充执行成功。本次未新增持久化错误日志或自动回归。

## V2-B026：用户排队输入绕过 Hook 和资源展开

- 2026-10-28 注册 transform input Hook，先 steer('queued-raw')、follow_up('/template-raw')，再 continue_：Hook 调用次数为 0，模型收到原始排队文本。源码中两个方法只构造消息、校验并入队；普通 prompt 又固定标记 source=sdk。Pi _queueUserInput 及 suite/agent-session-queue.test.ts 明确要求直接排队运行 input Hook、保留来源和投递方式，再展开资源；扩展命令不能当普通排队文本。
- 原因：只对齐队列的消费时机，漏了用户输入进入队列之前的处理过程。
- 状态：已定位，未修复；s10 README 已标注边界。按 s08/s10/s11 的能力分别补来源、Hook/命令检查、资源展开；不能将已构造的 custom 消息也误当用户命令重新处理。后续需覆盖 transform/handled、来源、扩展命令拒绝和资源只展开一次，保留既有工具批次及取消边界。
