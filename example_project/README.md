# 示例项目

每个子目录都是可单独指定为 `--workspace` 的目标项目。业务源码放在这里，索引缓存生成于各项目的 `.indexes/<章节>/`；会话和 Trace 仍在 zero2pi 根目录。讲解、预期答案和测试放在目标目录之外，缓存也不参与检索。

| 目录 | 用途 |
| --- | --- |
| [retrieval_demo/](retrieval_demo/) | s16 起默认使用的仓库检索示例：同名方法、装饰器、嵌套函数、跨文件调用和局部别名 |

后续示例按 `example_project/<项目名>/` 并列增加，不把多个项目的父目录当作默认搜索目标。

## 检索示例

```text
retrieval_demo/
├─ math_ops/calc.py   # Calculator.add；内部定义 normalize，把负数归零后相加
├─ storage.py         # Store.add；只返回包装字典，不写文件或数据库
└─ workflow.py        # checkout 直接调用；preview 通过 operation 别名调用
```

在 zero2pi 根目录运行，不需要安装示例依赖：

```powershell
# 直接验证示例业务：应输出 checkout: 5 和 preview: 9，不调用模型或 Docker
python -B -X utf8 example_project/retrieval_demo/workflow.py
# 先按 s16 README 手动准备镜像；文本模式检索定义、调用和赋值处
python -X utf8 -m s16_repository_search.code --workspace example_project/retrieval_demo --search add
# 先按 s17 README 手动准备含 ctags 的镜像；符号模式只查定义
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo --search add --search-mode symbol
# 只查运算目录；范围相对目标项目，不是相对 Harness
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo --search normalize --scope math_ops
# 启动只读 Agent；s16、s17 省略 --workspace 时也默认选此项目
python -X utf8 -m s17_repository_symbols.code --workspace example_project/retrieval_demo
```

可输入：

```text
只读分析：先用符号模式查 add 的两个定义，按实体范围补读；再用文本模式查它们在 workflow.py 中的使用位置。解释同名方法、嵌套函数与 operation 别名的区别，区分源码证据与尚未解析的调用关系。
```

## 观察位置

源码加了中文说明，因此行号与聊天中的简写不同，以这里实际文件为准：

| 源码 | 定义行 | 完整范围 | 观察点 |
| --- | --- | --- | --- |
| `math_ops/calc.py`：`Calculator.add` | 8 | 7–19 | 包含装饰器、说明和内部函数 |
| `math_ops/calc.py`：`Calculator.add.normalize` | 15 | 15–17 | 嵌套定义；正文匹配取最内层实体 |
| `storage.py`：`Store.add` | 7 | 7–9 | 同名但职责不同，不能按名字合并 |
| `workflow.py`：`checkout` | 7 | 7–15 | 第 13、14 行直接使用两个同名方法 |
| `workflow.py`：`preview` | 18 | 18–25 | 第 24 行取得方法，第 25 行通过别名调用 |

搜索 `add` 的文本模式会命中定义和 `workflow.py` 第 13、14、24 行；第 25 行没有这个文本，不会直接命中。按 `preview` 的范围补读可以看到别名调用，但 s17 尚未建立它到具体定义的调用关系。实际排名、省略与解析覆盖以引擎返回为准，这些说明不是实测日志或性能成绩。

s18 用“负数如何归零”观察语义召回，再通过 `normalize` 关键词观察混合排名；运行需要独立 embedding 配置与授权，见 [s18](../s18_semantic_retrieval/README.md)。s19 搜到 `Calculator.add` 的实体 ID 后，incoming/calls 应能给出 checkout 第 13 行与 preview 第 25 行的调用证据，见 [s19](../s19_relationship_navigation/README.md)。预期答案保留在本文件和测试中，不放进目标源码，不能把替身规则当成真实 embedding 质量结论。

相同源码复用同一代缓存。若要观察修改后的重建，可修改示例业务源码后重新搜索；不要手改索引。改回旧内容且相应缓存仍在时可复用旧代次。原先 zero2pi 根目录中的旧缓存不会自动迁移或清理。

## 自动化验证

```powershell
# 验证业务输出、默认目标、实体边界、文本/符号差异及索引复用和更新；使用后端替身
python -m pytest tests/test_retrieval_examples.py -k "not real_zoekt"
# 镜像手动准备后，显式验证同一示例在真实 s17 引擎中的定义/引用检索
$env:ZERO2PI_TEST_ZOEKT_S17 = "1"
python -m pytest tests/test_retrieval_examples.py -k real_zoekt_s17
Remove-Item Env:ZERO2PI_TEST_ZOEKT_S17
```

自动化测试将示例复制到临时目录再修改，索引与运行数据也写临时目录。预期文件和行号保存在 `tests/`，不进入示例项目的检索范围；普通离线测试不证明真实引擎性能。
