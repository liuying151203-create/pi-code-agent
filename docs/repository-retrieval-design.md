# 代码仓库理解与检索：第二阶段设计

更新：2026-10-21。s16–s19 按“确定性搜索 → 语义召回 → 代码关系 → 多路证据组装”重组；s20–s22 继续承接任务上下文、隔离修复和评测。真实引擎、容器和远程模型需要用户准备并授权，离线测试不能替代真实效果验证。

## 目标与章节边界

收到维护任务后，找到可引用的实现、测试与关联代码，并把有限上下文留给真正相关的证据。目标不是堆叠数据库，而是提升定位质量，同时衡量工具调用、Token、延迟与索引开销。

| 章节 | 一个主要机制 | 实际组件 |
| --- | --- | --- |
| [s16 文本与符号](../s16_repository_search/README.md) | Zoekt 与 ctags 共用索引；最小证据约定 | RepositoryIndex、EntityCatalog、CodeEvidence |
| [s17 语义召回](../s17_semantic_retrieval/README.md) | 结构切块、embedding 缓存和余弦召回 | CodeChunk、SemanticIndex |
| [s18 代码关系](../s18_relationship_navigation/README.md) | 静态关系与实体单跳导航 | RelationshipGraph、RelationshipIndex、RelationshipRetriever |
| [s19 多路融合](../s19_retrieval_fusion/README.md) | 召回编排、RRF、证据组装及可选关系补充 | FusionPolicy、EvidencePipeline、RepositoryRetrieval |
| s20 任务上下文 | 需求与本地 Git 信息组织 | TaskContextBuilder、GitReader |
| s21 隔离修复 | 修改、测试、反馈与确认交付 | AgentRuntime、RepairRunner |
| s22 定位评测 | 固定任务的策略消融与会话成本观察 | ComponentLocator、EvaluationRunner |

s16 已实际消费 CodeEvidence，才定义公共约定；s19 有多路结果需要合并，才引入排名和装包组件。旧章不放未来能力的空接口。

## 参考来源

- [Sourcebot 搜索实现](https://github.com/sourcebot-dev/sourcebot/blob/main/packages/web/src/features/search/zoektSearcher.ts)：参考 Zoekt 请求与结果适配的边界；文本和符号共用分片，不将其直接称为向量 RAG。
- [Zoekt ctags](https://github.com/sourcegraph/zoekt/blob/main/doc/ctags.md)：ctags 提供符号定义；不等于精确的跨语言调用图。项目固定 Zoekt 提交 27517a30ae6df16c6e768623847fdab8f6a3bbcf 与 ctags 6.1.0，用实际镜像 ID 区分缓存。
- [Potpie 历史发布](https://github.com/potpie-ai/potpie/releases)：参考结构解析、图与向量索引的分工；普通 dense 余弦不冒充 ColBERT/Qdrant。
- [Potpie 当前读取编排](https://github.com/potpie-ai/potpie/blob/main/potpie/context-engine/src/potpie_context_engine/application/services/read_orchestrator.py)：参考 reader、排序、结果组装职责。当前项目记忆 Context Graph 与历史静态代码图分开理解，不混用其版本或未实现的配置。
- 同级 Pi：工具与事件通过现有扩展 API 接入；同级 lcc：每章完整保留 agent_loop，显式展示主流程。检索是 Harness 的组合能力，不成为循环的新分支。

## 层次与组装

```text
Agent 的工具入口：search_code / related_code
                     │
               RepositoryRetrieval
               ├─ CodeRetriever：一次检索的显式编排
               │  ├─ Zoekt：文本、符号
               │  ├─ SemanticIndex：向量召回
               │  ├─ RelationshipIndex：可选一跳补充
               │  └─ EvidencePipeline：聚合、排名、预算组装
               └─ RelationshipRetriever：已知实体直接导航

共享结构基础：RepositoryWorkspace → SourceSnapshot → EntityCatalog
```

以上表示职责归属。组装细节见各章 README；箭头表示依赖或数据，流程图另行说明调用先后。

搜索先找到实体，关系再解释关联；已知实体时可以直接 related_code，不强制 embedding。关系属于仓库检索服务内部，但不是 Zoekt 或向量数据库的子功能。s19 的关系补充显式启用，最多前三个实体各五条直接调用者；关系不作为第四个 RRF 名次列表。

## 证据与一次检索

最小代码证据包含路径、命中范围、文件哈希、来源、可选实体和显示片段。函数的完整范围与某一处命中范围不同；同名函数靠实体身份区分。

s19 的主流程：

1. 验证参数和能力，固定同一次源码快照。
2. 从同版实体目录切块；文本、符号和语义各自召回。
3. 按“文件版本＋实体”聚合，保留同一实体的不同位置；一路每实体只贡献一次排名。
4. 使用加权 RRF：sum(weight / (k + rank))，默认 k=60、各路等权。
5. 可选补一跳关系，复用本次快照和实体目录。
6. 先组装主要文件，再用剩余预算装入关系；保留 sources、channel_ranks、coverage、omissions。
7. 复核源码指纹，变化就返回 stale；仅成功返回的证据登记补读哈希。

归一不等于抹平数据类型：代码片段保留位置和来源，关系边保留起点、终点、类型和证据行。不能将不同版本合并，也不能把结构邻居的文件顺序当作相关性名次。

候选预算与显示预算不同。不同位置先进入实体聚合，随后才按显示预算省略；Zoekt 的单次后端召回本身仍有文件/匹配数量上限。coverage 说明召回边界，omissions 说明显示省略，二者都不代表推理可信概率。

## 存储与安全

默认目标是 example_project/retrieval_demo，修复默认 repair_demo；标签在目标之外。自动化测试只改临时副本。

- 每章缓存位于实际目标的 .indexes/<章节>/，启动实际验证可写。
- Zoekt 代次包含 source 过滤副本、shards、entities.sqlite 和 manifest；源码变化整代重建，未变复用。
- 向量缓存位于 emb-<模型配置身份>/vectors.sqlite，只存输入哈希和向量。候选来自当前源码，不枚举缓存中的旧源码。
- 关系缓存位于 rel-<源码与解析版本身份>/relations.sqlite，只存节点、关系和版本。
- Session、Trace 与紧凑检索指标保留在 Harness，按章节和目标 ID 隔离，不从历史恢复远程发送授权。
- 源码读取、搜索、补读共用允许清单；缓存、保护文件与链接强制排除。s16–s20 的入口只读；s21 的修改位于隔离副本，交付需要确认。

每次请求仍扫描允许源码，返回前重读计算指纹但不积攒第二份正文。它不是文件系统锁，也不是后台增量索引；并发编辑可能触发明确失败，不作强一致承诺。

远程 embedding 需要 --allow-remote-embedding；记录索引/查询阶段的真实已知用量与未知用量，不自动安装或请求模型。所有显示结果有总字节预算，不把字符数当作 Token。

## 编号迁移说明

旧 s16 文本＋旧 s17 符号 → 新 s16；旧 s18 的语义部分 → 新 s17；旧 s19 关系 → 新 s18；旧融合逻辑 → 新 s19。

源码引用、命令和测试一起迁移。新 s16 镜像要求 ctags 标签与 s16-v2 适配器；旧镜像需要手动重建。旧缓存、历史会话、Trace 不自动搬迁或删除，不将新编号下的不同能力视作同一种缓存。s16 清理只识别当前完整格式，未知或旧格式保持不动。

## 优化与验证方向

当前实际可配置：FusionPolicy 的 RRF 常数/权重与显示预算；SemanticIndex 的 batch_size。切块仍是实体＋窗口，向量仍是精确搜索，关系仍是静态一跳。不会只增加策略名就声称实现了新算法。

后续按评测决定粒度，编号待定：父子块与重叠窗口、多语言解析、按文件复用结构结果、ANN、候选重排。Tree-sitter 扩展语法覆盖不等于解决完整语义绑定；这些能力需要独立运行路径和回归，不提前塞空实现。

s22 比较基础工具、文本、文本＋符号、混合、混合＋关系，使用同一冻结源码、输入和标签；记录 File Recall@K、延迟、冷/热索引开销与 embedding 用量。组件调用不冒充真实 Agent 工具调用，正式 SWE-bench/Agent 实验仍需数据和授权，未实测不写提升数字。
