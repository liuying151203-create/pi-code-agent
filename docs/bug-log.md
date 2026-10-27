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
