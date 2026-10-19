# s17：仓库系统 · 符号索引与代码实体

## 实现原理

s16 回答“哪些文件、哪些行出现了这个词”；s17 进一步回答“它是不是定义、属于哪个类或函数、完整代码从哪行到哪行”。仍是同一个 `search_code` 工具，默认 `mode="text"` 搜正文和引用，`mode="symbol"` 搜符号定义。

两部分各司其职：**Zoekt + ctags 负责查定义，Python AST 负责补代码边界**。ctags 的符号位置随 trigram 文本索引一起写入 Zoekt 分片；查询用 `Symbol(Substring)`，不是 Python 遍历名称来冒充符号索引。AST 只解析合规快照中的 `.py/.pyi`，提取类、函数、异步函数和限定名，不 import 或执行项目代码。

`EntityCatalog` 将 AST 结果保存为 `entities.sqlite`，和源码副本、Zoekt 分片按同一代发布。查询得到文件和行号后，再按行关联实体；命中嵌套函数体时取最内层实体。变量符号、非 Python 文件、模块级文本或解析失败的文件可能没有 AST 实体，结果明确为 `null`，不会编造边界。Python 解析错误另列，文本搜索仍可用。

## 整体工作示意

![s17：沿用 s16 的整体结构，在 Zoekt 中增加符号查询，在 Python 中增加实体关联和同代 SQLite 元数据](architecture.svg)

沿①–⑥查看“搜索 → 返回位置和实体 → 按需补读”的主线。虚线表示新建或重建时的写入；s17 新增内容在原有区域内标出，不增加另一套 Agent 循环或搜索服务。

## 运行与验证

在 zero2pi 根目录、已有虚拟环境下运行，先手动启动 Docker Desktop 的 Linux 服务。s16 镜像关闭了 ctags，**s17 需要单独构建一次**：

```powershell
# 手动下载构建依赖，编译固定 Zoekt 与 Universal Ctags 6.1.0；不包含目标项目
docker build -t zero2pi-zoekt:s17 s17_repository_symbols
# 不调用模型：查独立示例中两个 add 的定义，不查调用行
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo --search add --search-mode symbol
# 对照文本模式：定义和引用都可能命中；两种模式复用同一代索引
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo --search add
# 启动只读 Agent；复用根目录 .env 的 Provider 和上下文预算
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo
```

在 `s17 >>` 输入：

```text
只读分析：用符号模式查 add 的两个定义，按实体范围补读；再用文本模式查 workflow.py 中的使用位置。解释同名方法、normalize 嵌套函数与 operation 别名的区别，给出限定名、文件和行号，不把文本匹配当作已解析的调用关系。
```

观察 `search_code` 的 `entity.qualified_name/start_line/end_line`，以及随后 `read_file` 的补读范围。这里没有 bash 或写入工具，不需要逐条批准 shell。运行结果由当前源码和模型决定，上述任务不是实测日志。

目标默认是 `example_project/retrieval_demo`，不随启动目录改变，也不默认索引 Harness。多个独立示例统一放在 `example_project/`，源码结构与业务运行见 [示例说明](../example_project/README.md)。其他示例或已有项目加 `--workspace "D:\dev\my-app"`。`--session "<启动打印的 JSONL 路径>"` 只恢复同一目标的 s17 会话。修改目标源码不用重建镜像；修改 Dockerfile、search.go 或固定后端版本才需要重新构建。

索引写入 `<目标项目>/.indexes/s17/`。沿用 s16 的 `prepare_index_directory()`，启动时实际写入、删除临时文件，验证失败即停止，不回退到其他目录。业务源码只读；缓存由 Harness 写入，并强制排除出扫描和模型工具。外部 Git 项目请将 `.indexes/` 加入自己的 `.gitignore`。

```powershell
# 离线回归：后端和模型使用替身，不下载、不启动容器、不产生 API 费用
python -m pytest tests/test_s17_repository_symbols.py
# 镜像准备完成后，显式验证真实 ctags/Zoekt 的定义与引用区别，以及更新后的实体
$env:ZERO2PI_TEST_ZOEKT_S17 = "1"
python -m pytest tests/test_s17_repository_symbols.py -k real_zoekt
Remove-Item Env:ZERO2PI_TEST_ZOEKT_S17
```

离线测试验证编排和边界，不证明真实搜索引擎或 Agent 的定位性能。

## 组件与流程

| 组件 / 方法 | 本章职责 | 与 s16 的关系 |
| --- | --- | --- |
| `RepositoryWorkspace.prepare_index_directory/scan/read_file/dispatch` | 启动缓存验证、扫描、过滤、补读与搜索指纹校验 | 复用 s16；目标内缓存和 Harness 内记录均按 s17 隔离 |
| `ZoektBackend.build_index/search_index` | 开启 ctags；把 `mode` 传给薄 Go 适配器 | 修改：同一索引支持文本和符号查询 |
| `CodeEntity` | 一个类或函数的名称、完整范围、文件版本与 ID | 新增：元数据，不保存源码 |
| `EntityCatalog.parse/save/load/entity_at_line` | 解析定义、保存/恢复目录、按命中行找实体 | 新增：只做结构解析与关联，不重做关键词搜索 |
| `RepositoryIndex.prepare` | 分片和实体目录一起复用或发布 | 修改：返回的 `PreparedIndex` 增加 `entity_catalog` |
| `CodeRetriever.search/_build_file_results` | 扫描、准备索引、查位置、关联实体、复核版本、限制输出 | 修改：显式传递模式，并在命中中加入实体 |
| `RepositoryExtension.register/handle_search` | 注册同一个工具并接收 `mode` | 修改工具参数；指标事件与保存方式保持 |

先看 [code.py](code.py) 的 `main()` 组装 → [retrieval.py](retrieval.py) 的 `search()` 查询主线 → [index.py](index.py) 的 `prepare()` 缓存两条路径 → [entities.py](entities.py) 的解析与关联。完整 `agent_loop` 保留在本章，流程与 s16 相同。

```mermaid
flowchart TD
    input["Agent 调用 search_code，或命令行 --search"] --> scan["扫描合规源码，得到内存快照"]
    scan --> cache{"同一代缓存可用？"}
    cache -->|是| load["复用 Zoekt 分片，加载实体目录"]
    cache -->|否| build["写源码副本；Zoekt+ctags 建索引；AST 解析并保存实体"]
    build --> publish["全部成功后一起发布"]
    load --> query["Zoekt 按 text 或 symbol 搜索"]
    publish --> query
    query --> match["按命中行关联同代实体，复核源码版本与输出预算"]
    match --> result["返回文件、片段、实体范围和指纹"]
    result --> read["Agent 用 read_file 按需补读，再回答"]
```

### 查询示例

真实示例源码位于 [retrieval_demo](../example_project/retrieval_demo/)，不再以 Harness 本身为运行目标。`Calculator.add` 包含装饰器和局部函数 `normalize`，`Store.add` 是另一个同名方法；`checkout` 直接调用，`preview` 通过 `operation` 别名调用。

搜索 `add`：符号模式查到运算方法的定义行 `math_ops/calc.py:8`，以及存储方法的定义行 `storage.py:7`；文本模式还可能返回 `workflow.py:13`、`:14`、`:24`。第 25 行只有别名调用，不会直接命中这个文本。完整观察表见 [示例说明](../example_project/README.md#观察位置)，实际排序和省略以引擎为准。

相应命中的关键字段如下，省略了 ID 和哈希的实际值：

```json
{
  "start_line": 8,
  "end_line": 8,
  "reason": "符号定义匹配",
  "entity": {
    "path": "math_ops/calc.py",
    "name": "add",
    "qualified_name": "Calculator.add",
    "kind": "function",
    "start_line": 7,
    "definition_line": 8,
    "end_line": 19
  }
}
```

外层行号是**命中位置**，`entity` 内是**完整定义范围**。补读时调用 `read_file(path="math_ops/calc.py", start_line=7, limit=13)`；程序自动对照搜索指纹，文件已变化就拒绝沿用旧位置。文本命中局部函数第 15 行时关联到 `Calculator.add.normalize`；命中第 19 行则关联到外层 `Calculator.add`。关联只说明匹配位于谁的范围内，不建立调用关系。符号查询用区分大小写的名称片段 `add`，不是 `Calculator.add` 这种限定名表达式；限定名由结果提供，用于区分同名定义。

## 数据与组装结构

索引缓存放在实际目标项目中；例如 `--workspace "D:\dev\my-app"`：

```text
D:/dev/my-app/.indexes/s17/<索引代次>/
├─ source/          # 过滤后的源码副本，给 Zoekt 索引器读取
├─ shards/          # Zoekt 文本+符号分片，不是两份独立索引
├─ entities.sqlite  # Python 定义的身份、范围和解析错误；不保存源码正文
└─ manifest.json    # 源码指纹、目标、实际镜像 ID、AST/数据格式版本
```

默认缓存位于 `example_project/retrieval_demo/.indexes/s17/`，不再在路径中加项目 ID；不同项目的目录天然隔离，代次仍包含目标身份。原先 zero2pi 根目录下的缓存不会自动迁移、复用或删除。

`source_snapshot` 在内存保存本次合规文件的完整字节；磁盘 `source/` 是建索引的过滤副本，不是完整 Git 克隆。`entity_catalog` 在内存只保存元数据，SQLite 是它的持久化形式，不是另一份代码副本，也不负责向量搜索。

首次查询覆盖整个允许范围建立索引和实体目录；`scope` 只限制查询。未变化时复用，不因换词或换模式重建；源码变化时整代重建，不是逐文件增量。先在 `.build-*` 临时目录完成全部内容再改名，任何一步失败都不发布半成品或退回旧版本。查询前后仍扫描源码，开销边界沿用 s16。

`entity_id` 由项目、路径、限定名、定义行和文件哈希生成，所以同名定义不冲突，源码变化后 ID 也变化。数据库只存本章实际使用的实体和解析错误，没有提前加入向量或调用关系。

下面只画本章新增实体目录及直接关联；箭头统一表示提供依赖或数据，标签对应真实参数/字段，不表示执行顺序：

```mermaid
flowchart LR
    snapshot["SourceSnapshot"] -->|source_snapshot| index_manager["RepositoryIndex.prepare"]
    snapshot -->|snapshot| parse["EntityCatalog.parse"]
    parse -->|entity_catalog| prepared["PreparedIndex"]
    index_manager -->|index_dir / entity_catalog| prepared
    prepared -->|entity_catalog| results["CodeRetriever._build_file_results"]
    engine["Zoekt 命中位置"] -->|file_matches| results
    results -->|hits.entity| output["search_code 的 ToolResult.content"]
```

`main()` 的组装层次不变：`workspace + zoekt_backend → index_manager`，再将 `workspace + index_manager → retriever → retrieval_extension → extensions → agent_loop`。`EntityCatalog` 由管理器准备并放在 `PreparedIndex.entity_catalog` 中，不额外塞进循环。会话、Trace 和检索指标仍放在 **zero2pi 根目录**的 `.sessions/s17/<项目ID>/`、`.traces/s17/<项目ID>/`，不与 s16 共用会话文件或索引缓存。

## 范围与参考

最多 20 文件、每文件 3 个命中行、正文 16 KiB；扫描、路径保护和基础读取预算沿用 s16，AST 元数据另外限制 50000 个实体 / 64 MiB 数据库。超出容量明确失败；Python 语法错误有单独说明，其他语言可能有 Zoekt 符号结果但没有 AST 完整范围。ctags 不是完整编译器，不能保证识别所有语言或定义；`-require_ctags` 保证解析器可用及非超时解析故障不被静默忽略，但 Zoekt 的单文件解析超时仍可能跳过，`search_exhaustive` 不是符号覆盖率保证。

- [Sourcebot 的 Zoekt 搜索入口](https://github.com/sourcebot-dev/sourcebot/blob/main/packages/web/src/features/search/zoektSearcher.ts)利用同一引擎提供文本和符号定位；本章保留这一分工，不另写一套符号倒排搜索。固定 [Zoekt 提交](https://github.com/sourcegraph/zoekt/tree/27517a30ae6df16c6e768623847fdab8f6a3bbcf)，镜像加入 [Universal Ctags v6.1.0](https://github.com/universal-ctags/ctags/tree/v6.1.0)，保留相应许可证；s17 真实引擎测试验证定义与引用区别。
- 本章薄适配器只导出路径和命中行，完整 Python 范围由 AST 实体目录提供并同代发布；这是本项目的分工，不代表 ctags 本身不能提供作用域或结束位置。s18 用实体组织语义候选与混合检索，s19 再实现有证据的关系扩展。
- Pi 的扩展边界与 lcc 的渐进表达保持：工具接入不改循环，新增机制只在本章落地；s17 回归验证指标事件、只读补读及与 s16 相同的本地完整循环。
