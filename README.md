# Pi Code Agent

参考 Pi 设计、使用 Python 独立实现的编码 Agent 与可复用 Harness，提供模型接入、工具、会话、上下文管理和扩展机制；非 Pi 官方项目。

## 配置与运行

以下命令由你手动执行；首次安装可能下载依赖：

```powershell
# 进入项目，创建并激活独立环境
cd D:\dev\Agent\pi-code-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 安装正式包及测试工具，创建本地模型配置
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# 填写启动目录 .env 的模型、密钥和实际上下文容量后启动；进程环境优先，默认只读
python -m pi_code_agent --workspace .
# 宿主写操作逐次确认；不等于 OS 沙箱
python -m pi_code_agent --workspace . --mode default
# 显式加载可信 Todo 扩展
python -m pi_code_agent --extension pi_code_agent.extensions.todo
# 离线测试、规范检查和构建（构建不安装新依赖）
python -m pytest
python -m ruff check src tests tutorials
python -m pip wheel --no-deps --no-build-isolation -w dist .
```

`src/pi_code_agent/` 是正式实现，`tutorials/` 保存 s01–s17 的完整渐进教程，`tests/` 包含教程回归与 SDK 测试。正式包不导入教程。

外部产品的接入和组件关系见 [SDK](docs/sdk.md)；拆分范围与手动环境步骤见 [迁移说明](docs/migration.md)。
