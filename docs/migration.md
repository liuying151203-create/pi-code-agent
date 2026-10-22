# 拆分说明

基线：zero2pi `cb195884fd43dd8206525e216831c1e0d363a895`。原项目保留，不重置或删除。

- 正式实现进入 `src/pi_code_agent/`，不导入教程或仓库维护业务。
- s01–s15 位于 `tutorials/`，保留各阶段完整循环。根目录运行 `python -m tutorials.s01_agent_loop.code`。
- 章节代码是阶段快照；新产品统一使用正式 SDK 或公开运行组件。SDK 的工作区是实例属性，不改全局 CWD。
- 不直接复制原 `.git`；通过本地 Git bundle 继承原 `main` 的完整历史，不复制远程地址或 Git 配置。
- 不迁移 `.env`、虚拟环境、Session、Trace、索引、沙箱或本地 runtime。仅复制 `.env.example`；模型密钥需你手动配置。
- 未安装依赖、构建镜像或创建远程仓库；旧历史会话不自动混用。
- Docker Worker 路径改变，镜像需按 [沙箱说明](sandbox.md) 手动构建。测试替身通过不代表真实容器/API 已验证。

## Git 历史

两个新项目各自完整保留原来的 94 条提交，旧提交的 ID、作者、时间和消息不变；在此基础上各追加一条拆分提交。迁移完成时每个仓库有 95 条提交，拆分提交的父提交均为上述基线。

当前工作区只保留各自需要的文件，早期历史仍包含原项目全部章节。原路径可用 `git show <旧提交>:<旧路径>` 查看；Git 的重命名识别可以辅助追踪，但不保证拆分后的每个方法都能自动关联旧路径。

```powershell
# 在新项目目录检查拆分及原历史，不要重新初始化为孤立历史
git log --oneline -5
git show HEAD^ --no-patch --format=fuller
git status --short
```

新仓库没有配置 origin。创建对应的远程仓库后，再由你添加远程地址和推送；不要把它们推送到原 zero2pi 仓库。
