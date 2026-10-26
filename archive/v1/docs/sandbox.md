# 沙箱后端

A：容器隔离副本，确认后交付；B：容器绑定真实工作区，文件直接生效；C：原生受限进程，平台/runtime 准备要求沿用教程。

正式后端位于 `pi_code_agent.sandbox_copy`、`sandbox_shared`、`sandbox_native`。由宿主应用显式创建和清理，不能让模型选择 Docker 参数或偷偷降级到宿主执行。SDK 接入时一起提供工具目录、dispatch 和 permission_hook。

镜像只需要首次或 Worker/依赖变动时手动构建；会下载基础镜像和依赖。本次拆分未运行这些命令。

```powershell
# 在 D:\dev\Agent 目录执行，构建 A 的可信工具环境
docker build -f pi-code-agent/docker/sandbox-copy.Dockerfile -t pi-code-agent-sandbox:copy pi-code-agent
# B 在真实工作区运行，须先理解写入直接生效的边界
docker build -f pi-code-agent/docker/sandbox-shared.Dockerfile -t pi-code-agent-sandbox:shared pi-code-agent
```

容器无网络、不挂载宿主凭证；底座进程和可信 Python 扩展仍位于宿主，不能将其描述为整个 Agent 都在沙箱中。
