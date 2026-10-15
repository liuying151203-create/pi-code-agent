# s16：仓库系统 · Zoekt 文本检索

## 实现原理

Zoekt 把源码建立为带位置的 trigram（三字符）倒排索引，搜索时筛选候选并验证匹配。Python 负责目标范围、缓存更新、结果预算和扩展接入，不重写搜索算法。

Python 在宿主机组织检索，Zoekt 在短容器中建索引和查询。Docker 镜像准备的是 Zoekt 程序，不是目标仓库的源码或索引；模型只提供查询并阅读结果，不参与索引构建。`main()` 启动时只组装组件、检查后端，第一次调用 `retriever.search()` 才准备索引。

相对 s15，新增 `search_code`；与 `glob`、`grep`、`read_file` 共用一个目标项目。检索返回排序文件、行号和 `content_hash`，模型再按需读取源码。完整 `agent_loop` 保留在 [code.py](code.py)，没有检索业务分支。

## 运行与验证

在 zero2pi 根目录、已有虚拟环境下运行。先手动启动 Docker Desktop 的 Linux 服务，再**构建一次**镜像：

```powershell
# 手动下载基础镜像、固定版本 Zoekt 和 Go 构建依赖；只把 search.go 发入构建上下文
docker build -t zero2pi-zoekt:s16 s16_repository_search
# 不调用模型：检索当前项目中 latest_tool_details 的定义或引用
python -X utf8 -m s16_repository_search.code --search latest_tool_details --scope s15_stateful_extensions
# 启动只读 Agent；复用根目录 .env 中的 Provider 与实际上下文窗口配置
python -X utf8 -m s16_repository_search.code
```

在 `s16 >>` 输入：

```text
只读分析：先搜索 latest_tool_details，再读取相关源码，解释 Todo 状态从哪里恢复。给出文件和行号。
```

观察：`search_code` 返回位置和哈希，`read_file` 补读后模型解释；不会请求 bash 审批，因为本章不开放 shell 或写入工具。返回次序由模型和 Zoekt 决定，此处不是实测日志。

默认目标是 zero2pi；其他已有项目使用 `--workspace "D:\dev\my-app"`。`--session "<启动时打印的 JSONL 路径>"` 只恢复同一目标的 s16 会话。无需 `--mode`，本章固定只读。

修改目标源码**不需要重建镜像**：首次检索或允许文件内容变化时重建索引，未变化时复用。只有 Dockerfile、search.go 或固定 Zoekt 版本变化才需重新构建镜像；程序不自动下载、构建或启动 Docker 服务。

```powershell
# 离线回归：使用后端/模型替身，不启动容器、不产生 API 费用
python -m pytest tests/test_s16_repository_search.py
# 镜像手动准备后，显式验证真实 Zoekt 的查询、目录范围、更新、删除与人工标签
$env:ZERO2PI_TEST_ZOEKT = "1"
python -m pytest tests/test_s16_repository_search.py -k real_zoekt
Remove-Item Env:ZERO2PI_TEST_ZOEKT
```

普通回归不证明真实引擎性能；真实测试只验证小样本正确性，不是 SWE-bench 成绩或 Agent Token/工具调用提升。

## 组件与方法

- [workspace.py](workspace.py) `RepositoryWorkspace`：`scan()` 得到合规 UTF-8 快照；`dispatch()` 将基础读取绑定到同一目标；`read_file(expected_hash=...)` 拒绝用旧位置读取新版本。
- [index.py](index.py) `RepositoryIndex.prepare()`：管理索引的复用与重建，返回 `PreparedIndex`。新建时先写过滤副本，再调用 Zoekt 建分片，成功后发布；s16 是整代重建，不是逐文件增量更新。
- [zoekt.py](zoekt.py) `ZoektBackend`：`build_index()` 调用官方索引器，`search_index()` 查询磁盘索引。短容器无网络，只挂载过滤副本/缓存，不挂载原项目或 Docker socket。
- [retrieval.py](retrieval.py) `CodeRetriever.search()`：组织一次完整检索。`_build_file_results()` 用同版源码核验 Zoekt 的位置并组装片段，不重新搜索或排序。
- [extension.py](extension.py) `RepositoryExtension`：`register()` 注册工具与事件；`handle_search()` 接收工具参数并调用检索器。`content` 进入模型，`details` 保存检索指标。

阅读顺序：`main()` 看组件组装 → `CodeRetriever.search()` 看完整流程 → `RepositoryIndex.prepare()` 看缓存复用/构建两条路径；最后再看过滤和容器细节。

## 数据与存放位置

| 数据 | 存放位置 | 用途 |
| --- | --- | --- |
| 目标原始源码 | `--workspace` 指定的宿主机目录，默认是 zero2pi | 用户实际维护的项目，`scan()` 和 `read_file()` 从这里读取。 |
| `source_snapshot: SourceSnapshot` | Python 内存 | 第一次扫描的合规源码；`file_contents` 保存完整字节，`file_hashes` 保存逐文件哈希，`fingerprint` 是整体指纹，`skipped_counts` 是跳过计数。不是磁盘副本。 |
| 过滤后的源码副本 | 缓存的 `source/` 目录，宿主机硬盘 | 将快照字节写成文件，供 Zoekt 索引程序读取；不是完整 Git 克隆。 |
| Zoekt 索引分片 | 缓存的 `shards/` 目录，宿主机硬盘 | Zoekt 生成的真正检索数据，不是 Python 字典或源码路径清单。 |
| `prepared_index: PreparedIndex` | Python 内存 | 只保存分片路径 `index_dir`、代次 `generation` 和本次是否重建 `rebuilt`；不保存源码或索引本体。 |
| `zoekt_result` | Python 内存 | 后端返回的位置与完整性标记，用于组织结果，不直接整份保存到会话。 |
| `search_result` / `search_metrics` | Python 内存；通过返回值交给会话/事件层 | 前者序列化为给模型的正文；后者通过 `ToolResult.details` 传递耗时等指标。 |
| `latest_snapshot` | Python 内存 | 查询结束前再次扫描的源码，只用整体指纹检查查询期间是否有变化。 |

磁盘缓存统一位于 **zero2pi 根目录**，不跟随 `--workspace` 移到外部目标项目。以 zero2pi 位于 `D:\dev\Agent\zero2pi` 为例，构建成功后是：

```text
D:\dev\Agent\zero2pi\.indexes\s16\<项目ID>\<索引代次>\
├─ source\          # 过滤后的源码副本，保留相对目录结构
├─ shards\          # Zoekt 索引文件（.zoekt）
└─ manifest.json    # 对应的源码哈希、目标身份和后端版本
```

项目 ID 根据目标规范路径生成，用于隔离项目；索引代次根据源码及后端版本等计算，用于复用同一版数据。构建时先写临时 `.build-*` 目录，成功后再改名发布，避免把半成品当作有效索引。

容器不挂载目标原项目：建索引时把临时源码副本只读挂为 `/source`，索引输出目录可写挂为 `/index`；查询时只把已发布的 `shards/` 只读挂为 `/index`。这些是宿主机目录的挂载，不会打进镜像，容器退出后已发布缓存仍保留。

## 运行流程

一次 `search_code` 调用经 `RepositoryExtension.handle_search()` 进入 [CodeRetriever.search()](retrieval.py)。直接使用 `--search` 也会进入同一检索方法，只是不经过模型和扩展。以下以允许范围内存在源码文件为例：

1. **扫描源码到内存**：`workspace.scan()` 先过滤再读文件，得到 `source_snapshot`。此时尚未写磁盘副本，也没有调用索引器；未提交修改同样包含在快照中。
2. **准备索引**：`index_manager.prepare(source_snapshot)` 根据快照和后端版本查缓存。同代次缓存存在且校验通过就复用；无对应代次则把内存字节写成临时 `source/`，调用 `backend.build_index()`。它运行官方 `/zoekt-index`，将真正的索引写入 `shards/`，成功后发布并返回 `PreparedIndex`。已有缓存损坏会明确报错，不自动覆盖。
3. **查询索引**：`backend.search_index(prepared_index.index_dir, query, scope, limit)` 读取磁盘索引；[search.go](search.go) 打开 `/index`，构造字面查询并调用 Zoekt 的 `Search()`。返回的 `zoekt_result` 主要是排序文件、行号和完整性标记，不是整份源码。
4. **核验并组织片段**：`_build_file_results()` 将后端位置与第一次扫描的 `source_snapshot` 对照，检查该行确实包含查询，再截取显示片段、附上文件哈希；不重新搜索或排名，也不改用新版源码解释旧位置。
5. **检查期间是否变化**：再次 `workspace.scan()` 得到 `latest_snapshot`，与第一次快照比较指纹。不同则清空结果并返回 `stale`；这次扫描只检查变化，不会再次建索引或搜索。
6. **返回结果**：限制 JSON 正文大小，返回 `ToolResult(content=正文, details=指标)`。交互模式沿用 s15 保存会话、发送事件；模型只得到少量位置和片段，需要更多源码时再用 `read_file` 补读原项目，传 `expected_hash` 可再次检查版本。

```mermaid
flowchart TD
    Q[retriever.search] --> S[scan：原项目磁盘 → 内存快照]
    S --> I[prepare：查找同代次缓存]
    I -->|已有缓存校验通过| Z[search_index：查询磁盘 shards → 文件与行号]
    I -->|无对应代次| W[内存字节 → 临时 source 副本]
    W --> C[build_index：Zoekt 生成 shards 后发布]
    C --> Z
    Z --> B[_build_file_results：用首次内存源码核验并截取片段]
    B --> V[再次 scan → latest_snapshot，比较两次指纹]
    V -->|源码未变| R[返回文件 行号 哈希和有界片段]
    V -->|源码变化| F[返回 stale 不使用旧证据]
    R --> P[沿用 s15 保存正文和指标后发送事件]
    P --> M[模型按需 read_file 并回答]
```

例如目标是 `D:\dev\my-app`，其中 `src/calc.py` 第 1 行为 `def add(a, b):`。首次搜索 `add` 时：该行字节进入内存快照 → 写入 zero2pi 缓存副本的 `source/src/calc.py` → Zoekt 建索引并找到 `src/calc.py:1` → Python 从首次快照取出 `def add(a, b):`，附上哈希 → 再扫描确认未变后返回。若期间被改成 `subtract`，则返回 `stale`，不提供旧证据。此例是流程示意，不是运行日志。

随后搜索 `return`，只要允许文件的路径/内容、后端版本等没变，就复用同一版索引，不重写副本；索引不是专门为 `add` 建立的。当前实现每次正常检索前后仍会完整读取允许范围内的源码，索引没有省掉版本检查所需的扫描。两份快照可能同时占用内存，32 MiB 是单次源码字节上限，不是进程总内存上限。

无匹配是 `empty`；搜索不完整是 `partial`；源码变化是 `stale`；扫描、构建或协议错误是 `failed`；参数错误是 `invalid`。`truncated` 表示显示内容有省略，`search_exhaustive` 表示后端是否完成搜索，二者不同。`empty` 仅针对允许索引的范围，跳过计数另列。

## 组装结构

箭头表示左侧向右侧提供依赖或数据；标签对应真实参数/注册方法，不表示执行先后。

```mermaid
flowchart LR
    W[workspace: RepositoryWorkspace] -->|workspace| I[index_manager: RepositoryIndex]
    B[zoekt_backend: ZoektBackend] -->|backend| I
    W -->|workspace| R[retriever: CodeRetriever]
    I -->|index_manager| R
    R -->|retriever| X[retrieval_extension: RepositoryExtension]
    P[metrics_path] -->|metrics_path| X
    X -->|register_tool: search_code / on: tool_result| A[extensions: s15 ExtensionAPI]
    W -->|base_tools / base_dispatch / permission_hook| A
    A -->|tools / dispatch / hooks| L[本章 agent_loop]
    A -->|subscribe: handle_event| E[events: EventDispatcher]
    E -->|emit| L
```

运行记录按目标规范路径哈希分组：`.sessions/s16/<项目ID>/`、`.traces/s16/<项目ID>/`、`.indexes/s16/<项目ID>/`。检索指标在 Trace 旁的 `*.retrieval.jsonl`，普通 Trace 不重复保存完整 details。恢复会话不恢复授权或索引状态；旧缓存留作可重建数据，不写回原仓库。

## 范围与参考

本章只搜区分大小写的单行字面文本；`scope` 是相对目录，省略表示整个允许范围。最多 20 个文件、每文件 3 个匹配行、正文 16 KiB；单文件 1 MiB、源码总量 32 MiB、5000 个合规文件/20000 个目录项，超过总容量明确失败。排除常见凭证、依赖、运行记录、路径别名、二进制及非 UTF-8；Git 项目在读正文前应用 Git ignore。任意文件中的秘密无法靠名称识别；这是只读应用策略，不是宿主 OS 沙箱，扫描也不是原子快照。

- [Sourcebot](https://github.com/sourcebot-dev/sourcebot/blob/main/packages/web/src/features/search/zoektSearcher.ts) 用 Zoekt 返回有范围和位置的结果。本章保留这一职责，改为短容器而非常驻 gRPC/Web 服务，降低部署复杂度；更新、范围、结果完整性由 s16 回归验证。
- 复用 [Zoekt](https://github.com/sourcegraph/zoekt/tree/27517a30ae6df16c6e768623847fdab8f6a3bbcf) 固定提交 `27517a30ae6df16c6e768623847fdab8f6a3bbcf` 的索引和搜索 API；Go 文件只构造字面查询、约束范围并导出位置，保留上游 LICENSE。s16 关闭 ctags，s17 再实现 Python 符号边界与文本命中映射。
- Pi 提供扩展注册边界，本章继续用 s15 API，不修改循环。lcc 强调渐进章节；本章只引入文本索引，不提前加入 Potpie 式向量和代码关系数据。语义融合与关系扩展分别由 s18、s19 实现和验证。
