# Pi Code Agent

参考 Pi 设计、使用 Python 实现的异步 Agent 与可复用 Harness；当前新主线完成 s01–s12。

## 环境与首次运行

以下环境和安装命令由你手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# 无密钥离线运行
python -m pi_code_agent --demo --workspace example_project/mini_app --prompt "读取 README.md"
# 在 .env 填写模型与密钥后，使用真实模型
python -m pi_code_agent --workspace example_project/mini_app
```

## 开发命令

```powershell
python -m pytest
python -m ruff check src tests tutorials examples
python -m ruff format --check src tests tutorials examples
python -m pip wheel --no-deps --no-build-isolation -w dist .
```

`src/` 是正式实现，`tutorials/` 按章保存独立的完整教学快照，`tests/` 是新主线测试，`examples/` 是 SDK 示例，`example_project/` 是独立练习项目。旧版完整保存在 `archive/v1/`，不参与新包导入、构建或默认测试。

从 [教程](tutorials/README.md) 开始；接口见 [SDK 用法](docs/sdk.md)，旧版说明见 [归档](archive/README.md)。
