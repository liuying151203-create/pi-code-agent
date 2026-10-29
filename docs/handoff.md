# 新主线交接

## 2026-10-29：s07 开工

原 s07–s12 已机械顺延为 s08–s13，目录、测试引用及冻结清单已更新；它们尚未在各自的新前一章上接入压缩。顺延后的全量测试为 1653 passed、8 skipped。

新的 s07 已从 s06 复制独立快照，并在正式包与快照增加切点计算和离线测试：按近期预算反向查找用户或模型消息，避免从工具结果开始，并识别单个回合的切分。当前尚未实现摘要、会话记录迁移、内存记录、SDK/CLI `/compact`、自动触发和溢出恢复，不应将 s07 当作完成。后续按 [上下文压缩设计](context-compaction-design.md) 先完成手动压缩和恢复，再做自动触发；测试失败或结果不确定不能发布压缩记录。

## 2026-10-28：上下文压缩设计

用户已学到 s06，要求优先完成核心压缩设计。按 Pi 锁定版本复核压缩实现、会话记录与相关测试，形成 [上下文压缩设计](context-compaction-design.md)，并修订 [roadmap](roadmap.md) 和教程索引。用户要求教学顺序与编号一致，并认可 s07 教学入口使用简单判断触发 `/compact`。手动和自动压缩合并为 s07，同章分两阶段；原扩展基座顺延为 s08–s13，压缩 Hook 在 s09 实现。这次只完成设计修订，尚未实现或验证压缩代码。

s07 快照从 s06 演进，s08–s13 顺延原 s07–s12 并按新前一章逐章接续。旧目录、测试引用和来源标记尚未迁移；先完成机械顺延并验证已有行为，再实现 s07。正式源码在已有扩展基座上继续演进。设计明确 version=1 读取、首次压缩迁移与保留备份、无盘会话、模型上下文窗口、估算/累计区分、工具安全边界、失败取消/溢出及扩展 Hook 的验收。

下文是前面各轮的历史记录；其中旧教学进度和“压缩编号待定”已被本节及新设计取代。

## 2026-10-28：第二项——基础兼容跨章修复

正式包和 s04–s12 回补 s03 已有的 thinking、签名和脱敏数据处理；s05 起的真实 SDK 流能够累积这些块，错误/取消保留已收到的快照；s06 起的会话编码保存并恢复推理块，旧文本/工具记录仍可读取；s08 起 context Hook 可传递它们。公开 SDK 导出 Thinking。终端仍只展示 Text，不启用推理预算或显示推理。带新增 thinking 块的文件不能被旧版程序读取。

s04 的两种协议现在跳过 error/aborted 回答，仅影响请求投影，本地失败历史保留。s04–s12 和正式包将模型 SDK 与 dotenv 移到实际使用处；help/demo、注入客户端不会加载模型 SDK，缺失对应 SDK 时报告 ProviderError。V2-B019 的跨章遗漏、V2-B022、V2-B023 已修复，s09 命令与 s10 队列问题仍待后续教学修订。

全量测试 **1653 passed, 8 skipped in 61.79s**；单独的流式/保存/Hook thinking 回归 65 项通过，隔离依赖入口 55 项通过。默认 Python（Anthropic 1.5.0，未安装 openai）实际通过 s04 demo、正式包 help，以及真实 SDK 累积器的离线流式 thinking/签名回传；只替代网络，没有调用真实模型。冻结清单仅更新受影响的 34 份 s04–s12 config/messages/providers/session 文件，第一项另外更新 10 份 CLI 快照。

实际验证命令（basetemp 已使用，重跑换新目录）：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp .sessions/all-6b859e56 -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m pytest tests/test_s05_streaming.py tests/test_s06_sessions.py tests/test_s08_hooks.py -k thinking --basetemp .sessions/cs-35c8ccc3 -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m pytest tests/test_config_cli.py -k 'offline_entry or model_construction or injected_clients' --basetemp .sessions/dep-83932ece -p no:cacheprovider --tb=short
python -B -m tutorials.s04_events.code --demo --workspace example_project/mini_app --prompt 'read README.md'
$env:PYTHONPATH='src'; python -B -m pi_code_agent --help
```

两个阶段都未提交。第一项补丁保存在 `.sessions/delivery-c1e10cb7/01-usage.patch`，可先用 `git apply --cached` 应用于暂存区并提交；再暂存剩余已跟踪修改，独立提交第二项。

## 2026-10-28：第一项——本次输入用量累计

正式 CLI 与 s03–s12 在循环返回后，只累计本次新增回答的输入/输出 token；部分缺失明确标记、全部缺失提示未知、有效零值正常显示，demo 不编造统计。SDK 消息接口和核心循环不变。V2-B021 已修复；会话累计、缓存和费用仍未加入。

新增 80 项完整/流式、多请求、连续输入及缺失/零用量回归通过，s03/s04 未实现流式的 8 项跳过。连同既有模型/CLI/边界/快照验证 **294 passed, 8 skipped in 25.38s**，Ruff 检查与格式检查通过。只更新 s03–s12 的 cli.py 冻结哈希。

实际验证命令（basetemp 已使用，重跑换新目录）：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest tests/test_s03_models.py tests/test_config_cli.py tests/test_boundaries.py tests/test_tutorial_snapshots.py --basetemp .sessions/us-d89c3249 -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m ruff check --no-cache src/pi_code_agent/cli.py tests/test_config_cli.py tutorials
..\zero2pi\.venv\Scripts\python.exe -B -m ruff format --no-cache src/pi_code_agent/cli.py tests/test_config_cli.py tutorials
```

## 2026-10-28：规划与 Pi 对齐复核

本次基线 c9d5e32，检查当前实现、章节规划和 Pi 原锁定提交 e4c75a732。Pi 工作区 HEAD 已更新为 72363383f，复核通过 git show 读取锁定版本，未升级对齐基准。完整结论和分阶段验收见 [Pi 对齐与规划复核](pi-alignment-review.md)，调整后的顺序见 [roadmap](roadmap.md)。

已确认但尚未修复：用量只显示最后一次请求、thinking 修复未覆盖正式包和后续章、s04 重放失败回答、同名扩展命令丢失、命令异常没有诊断、用户排队输入绕过预处理。分别记录 V2-B021–V2-B026；s09/s10 README 已更正对齐边界。本轮只改文档，不改源码/快照、不安装依赖、不调用真实模型、不提交。

现有相关测试 **312 passed in 23.16s**；另外通过 Python -B 内联脚本做离线复现，只替换网络边界，真实调用章节/产品组件。312 项不包含本次缺口的新增自动回归，不能理解为问题已解决。实际测试命令如下，复跑请换新的专用 basetemp：

```powershell
..\zero2pi\.venv\Scripts\python.exe -B -m pytest tests/test_s03_models.py tests/test_s09_commands.py tests/test_s10_queues.py tests/test_s11_resources.py tests/test_config_cli.py tests/test_boundaries.py tests/test_tutorial_snapshots.py --basetemp .sessions/aud-54eb8b51 -p no:cacheprovider --tb=short
```

下文是 2026-10-27 批次的历史验收记录；其中“s01–s05 未变”等结论限于当时批次，不代表后续 Bug 修订后的现状。当前教学已回到 s03 复核，继续按教学进度推进，不能照旧记录直接跳到 s06。

## 2026-10-27：扩展基座批次

2026-10-27：用户确认完成 s06–s12 扩展基座，并明确授权每章实际提交、提交后复查与独立修复。基线 be53c3b（s05）；本批代码与最终跨章修复到 6d9e6dc。没有安装依赖、初始化环境、请求真实模型或推送远端。

## 已完成与提交

| 章节 | 可验收功能 | 实现提交 | 复查修复 |
| --- | --- | --- | --- |
| s06 | 消息所有者、线性 JSONL、保存/恢复与持久化屏障 | d78cae8 | 3859d21 |
| s07 | 可信工厂加载、事务、工具注册优先级、模块清理 | d423a36 | 27cdd3a |
| s08 | 输入/上下文/工具 Hook、失败分流、结果覆盖 | e3fdaa1 | 990e897 |
| s09 | 命令/参数、UI/状态/模型消息/触发回合分离 | 8fab3ae | a519e69 |
| s10 | 取消、空闲屏障、steering/follow-up 与投递边界 | 351fc71 | 9023750 |
| s11 | 业务上下文、Skill 元数据/显式读取、模板参数 | 252680e | 5860514 |
| s12 | 动态工具快照、启停代次、扩展/资源重载与回滚 | 32f6c8d | 4939c87 |

最终跨章修复 6d9e6dc：轮数上限不丢已出队消息，input handled 与取消路径也投递不触发回合的消息。只向 s10–s12 回补已复现 Bug，没有加入后章组件；冻结哈希逐项更新。复现、原因、边界与回归见 [bug-log.md](bug-log.md) 的 V2-B006–V2-B014。

## 当前结构与行为

正式源码只在 src/pi_code_agent，外部程序使用 pi_code_agent；教程不被正式包导入。12 章共 153 个冻结 Python 源文件，s01–s05 与基线完全相同，archive/v1 无变更。教程入口见 [tutorials](../tutorials/README.md)，SDK 实际用法见 [sdk.md](sdk.md)。

- AgentSession 持有历史、运行状态、队列和扩展代次；SessionStore 负责同步 JSONL 屏障。pending 流快照不落盘，保存失败阻止后续运行。文件为 Python version=1 线性格式，不直接导入 Pi 会话。
- ExtensionRuntime 加载可信 Python 工厂。内置 → 跨扩展首个同名 → SDK 显式覆盖；同一扩展同名最后注册。失败工厂不发布注册，旧 API/工具/上下文在失效后拒绝继续操作。
- input 可 continue/transform/handled；context 只改当前请求，不改持久历史。tool_call 在参数校验后执行，异常阻止副作用；普通 Hook 错误进入 diagnostics，原生取消继续传播。tool_result 可改正文、错误状态和 terminate。
- 命令先于 input Hook；返回值不自动交给模型。notify 只显示，append_entry 只存状态，send_message 的 display、trigger_turn、deliver_as 分别决定行为。custom 消息会转换为模型用户文本，custom 状态条目不会进入上下文。
- 双队列默认 one-at-a-time，可各自选择 all。完整工具批次后投递 steering，准备停止时才投递 follow-up。忙时不触发回合的消息等安全边界保存；next_turn 等下次输入。取消补齐未完成工具结果，wait_for_idle 等末事件与保存完成。
- ResourceLoader 明确注入，不由 SDK 隐式扫描。默认上下文根为业务工作区；可显式指定 context_root 继承祖先。全局目录由 CLI 的 PI_CODE_AGENT_HOME 指定。Skill 正文按需读取，模板一次替换，参数不递归求值。
- 动态工具在下一次模型请求固定定义和执行目录，同批工具不因切换突然消失。重载先加载候选，再关闭旧代次，失败保留旧集合；CLI 会重新发现入口并重建 Skill 读取范围。借用的模型客户端始终由创建者管理。

## Pi 参考与边界

锁定本地 Pi e4c75a73222ae2c72abb5f5314fa35ee8effc508（0.85.1），没有把实验分支或其他版本行为混入对齐结论。主要源码为 agent/src/agent.ts、agent-loop.ts，coding-agent/src/core/agent-session.ts、session-manager.ts、extensions/loader.ts/runner.ts、resource-loader.ts、skills.ts、prompt-templates.ts；测试重点包含输入/工具 Hook、工具下一回合刷新、模板替换、取消和队列。来源许可见 [third-party-notices.md](third-party-notices.md)，每章也附 NOTICE.md。lcc 仅参考渐进教学与运行示例风格。

明确差异：Python 使用 Pydantic、PyYAML safe_load、asyncio 取消及模块命名空间；业务上下文范围比 Pi 默认祖先继承窄；重载先验证候选比 Pi 当前先关闭旧代次更保守。回滚只覆盖注册和资源集合，无法撤销可信工厂自行产生的外部副作用。扩展不是权限沙箱，阻塞同步 Python 不能被强制取消。

本轮不包含会话树/切换/分支、压缩恢复、完整 TUI、并行工具、MCP、动态供应商注册、全部 Pi Hook、多模态、完整 ignore 文件、包管理资源和 worktree 遮蔽；这些在各章标明编号待定。完整范围见 [规划](extension-foundation-plan.md)。

## 实际验证

最终 **1350 passed in 32.64s**；Ruff 检查通过，216 个 Python 文件格式检查通过。12 项离线 CLI/SDK 冒烟覆盖正式帮助/运行、s06–s12 独立入口、s09 扩展命令与参数、s12 /reload、完整 SDK 例子。测试只替代网络边界，不声称完成真实模型联网验证。

另在独立临时副本中用已有 bundled Python / setuptools 84.1.0 构建 pi_code_agent-0.2.0-py3-none-any.whl：21 个条目，只包含正式包与元数据，带 PyYAML 依赖和第三方许可，不带教程、归档或测试。构建产物随临时目录清理，没有安装包。

测试环境：已有 ..\zero2pi\.venv 的 Python 3.12.10、Pydantic 2.13.5、Anthropic 1.6.0、OpenAI 3.19.0、dotenv 1.2.3、PyYAML 6.0.3、pytest 9.1.1、Ruff 0.16.8。PyYAML 只补项目依赖声明，本轮未执行安装。

实际执行的核心命令如下。已用的 pytest 目录是 .sessions/final-7261de6f；重跑时必须使用新的随机专用目录，不能复用该路径：

```powershell
$p = '.sessions/final-' + [guid]::NewGuid().ToString('N').Substring(0,8)
..\zero2pi\.venv\Scripts\python.exe -B -m pytest --basetemp $p -p no:cacheprovider --tb=short
..\zero2pi\.venv\Scripts\python.exe -B -m ruff check --no-cache src tests tutorials examples example_project
..\zero2pi\.venv\Scripts\python.exe -B -m ruff format --check --no-cache src tests tutorials examples example_project
$env:PYTHONPATH = 'src;.'
..\zero2pi\.venv\Scripts\python.exe -B examples/extension_foundation_sdk.py
..\zero2pi\.venv\Scripts\python.exe -B -m pi_code_agent --demo --no-session --no-extensions --workspace example_project/mini_app --prompt '读取 README.md'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s09_commands.code --demo --no-session --no-extensions -e tutorials/s09_commands/example_extension.py --label=教程 --prompt '/note 测试命令'
..\zero2pi\.venv\Scripts\python.exe -B -m tutorials.s12_reload.code --demo --no-session --no-extensions --workspace example_project/mini_app --prompt /reload
git -c core.safecrlf=false diff --check
git diff --quiet be53c3b -- archive
```

自动化测试通过 autouse fixture 隔离 PI_CODE_AGENT_HOME，独立快照复制后阻断产品与其他教程导入再运行。再次测试保持这一隔离，避免执行用户全局扩展。最终快照哈希全部通过，旧章只允许明确的 Bug 修订。

## 后续教学

基座代码完成，教学仍按章推进。先从 s06 解释消息所有权和落盘屏障，再到注册/Hook；s09 重点亲自验证四种效果分离。每章能说明输入、输出、状态和失败路径并通过 README 验证后，再进入下一章。新业务功能优先作为扩展实现；后续专题先确认范围和编号，不提前塞未使用组件。
