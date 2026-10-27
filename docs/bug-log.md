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
