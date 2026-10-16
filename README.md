# zero2pi

`zero2pi` 是一个从零实现 Agent 的学习与实践项目：以 `learn-claude-code` 的渐进式方式组织开发，参考 `pi` 的 Agent loop、工具调用和运行时设计，逐步构建一个清晰、可运行、可扩展的 Agent Harness。

第二阶段：[代码仓库理解与检索设计](docs/repository-retrieval-design.md)；[s16：Zoekt 文本检索](s16_repository_search/README.md)。

## 基础环境

下面的命令分别用于创建虚拟环境、安装项目依赖，以及创建本地配置文件：

```powershell
# 创建项目独立的 Python 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 安装项目依赖和开发工具
python -m pip install -e ".[dev]"
# 创建本地环境变量文件
Copy-Item .env.example .env
```

在 `.env` 中填写模型密钥。若 PowerShell 不允许激活虚拟环境，可直接使用 `.venv\Scripts\python.exe`。

s07–s16 的模型交互还需填写实际 `MODEL_CONTEXT_WINDOW_TOKENS`；预算迁移与示例见 [s07 配置](s07_session_compaction/README.md#运行)，不要直接复制未经服务确认的模型容量。

s13-A 另需手动准备 Docker Linux 服务和本地镜像，命令见 [Sandbox 教程](s13_a_sandbox_copy/README.md#运行与验证)；程序不自动拉取镜像或安装依赖。

直接写入真实项目的两种方案：[s13-B 共享容器](s13_b_sandbox_workspace/README.md)、[s13-C 原生受限进程](s13_c_sandbox_native/README.md)。启动需显式授权，分别手动准备镜像或原生 runtime。

## 常用命令

```powershell
# 运行第一章：核心循环
python -m s01_agent_loop.code
# 运行第二章：工具调用
python -m s02_tool_use.code
# 运行第三章：工具权限
python -m s03_permission.code
# 第三章只读分析：仅开放查找、搜索和读取工具
python -m s03_permission.code --mode read-only
# 运行第四章：工具调用 Hooks
python -m s04_hooks.code
# 第四章只读分析：经权限 Hook 接入 s03 的只读策略
python -m s04_hooks.code --mode read-only
# 运行第五章：会话系统 · 持久化
python -m s05_session_persistence.code
# 第五章只读对话：限制模型工具，继续保存会话记录
python -m s05_session_persistence.code --mode read-only
# 加载已有的第五章会话
python -m s05_session_persistence.code --session .sessions\s05\session-20260919-120000.jsonl
# 运行第六章：会话系统 · 记录与上下文投影
python -m s06_session_context.code
# 加载已有会话并按 s06 Entry 规则构建模型上下文
python -m s06_session_context.code --session .sessions\s06\session-20260919-120000.jsonl
# 运行第七章：会话系统 · 上下文压缩
python -m s07_session_compaction.code
# 加载已有的第七章会话
python -m s07_session_compaction.code --session .sessions\s07\session-20260919-120000.jsonl
# 运行第八章：运行时系统 · 事件边界
python -m s08_runtime_events.code
# 加载已有的第八章会话
python -m s08_runtime_events.code --session .sessions\s08\session-20260922-120000.jsonl
# 运行第九章：运行时系统 · 流式响应
python -m s09_runtime_streaming.code
# 加载已有的第九章会话
python -m s09_runtime_streaming.code --session .sessions\s09\session-20260922-120000.jsonl
# 运行第十章：模型系统 · Provider 边界
python -m s10_model_provider.code
# 运行第十一章：运行时系统 · 追踪与用量
python -m s11_runtime_observability.code
# 运行第十二章：能力系统 · Skills 按需加载
python -m s12_skill_loading.code
# 只读审查：s03–s12 均支持此模式，仅开放 glob、grep、read_file
python -m s12_skill_loading.code --mode read-only
# 第十三章 A：隔离副本；/diff 预览，/apply 确认回写
python -m s13_a_sandbox_copy.code --mode sandbox
# 第十三章 A：指定其他项目；配置和按项目分组的记录仍留在 zero2pi
python -m s13_a_sandbox_copy.code --workspace "D:\dev\my-app"
# 不调用模型，验证第十三章 A 的容器与工具链路
python -m s13_a_sandbox_copy.code --smoke
# 第十三章 B：共享真实项目；先按 B 教程准备镜像，文件直接生效
python -m s13_b_sandbox_workspace.code --workspace ".\example_project\b_demo" --allow-write
# 第十三章 C：原生受限进程；先按 C 教程准备 runtime 和平台初始化
python -m s13_c_sandbox_native.code --workspace ".\example_project\c_demo" --allow-write
# 第十四章：加载受信任 Python 扩展，示例工具复用只读权限
python -m s14_extensions.code --mode read-only
# 第十五章：Todo 扩展，自然语言查询清单、事件自动提示进度
python -m s15_stateful_extensions.code --mode read-only
# 第十六章：先按章节 README 手动构建 Zoekt 镜像，再只读定位源码
python -X utf8 -m s16_repository_search.code
# 不调用模型，单独验证检索；--workspace 可指定其他已有项目
python -X utf8 -m s16_repository_search.code --search latest_tool_details --scope s15_stateful_extensions
# 第十七章：按章节 README 单独构建含 ctags 的镜像，再查询符号定义与实体范围
python -X utf8 -m s17_repository_symbols.code --search restore_state --search-mode symbol --scope s15_stateful_extensions
# 第十七章只读 Agent：文本/符号模式由 search_code 工具参数选择
python -X utf8 -m s17_repository_symbols.code
# 运行测试
python -m pytest
# 检查代码规范
ruff check s01_agent_loop s02_tool_use s03_permission s04_hooks s05_session_persistence s06_session_context s07_session_compaction s08_runtime_events s09_runtime_streaming s10_model_provider s11_runtime_observability s12_skill_loading s13_a_sandbox_copy s13_b_sandbox_workspace s13_c_sandbox_native s14_extensions s15_stateful_extensions s16_repository_search s17_repository_symbols src tests
```
