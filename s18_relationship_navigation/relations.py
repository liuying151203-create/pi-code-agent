"""s18：仅解析 Python 的明确绑定，未知关系不靠同名猜测。"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from s16_repository_search.entities import EntityCatalog
from s16_repository_search.workspace import SourceSnapshot, source_lines

MAX_RELATIONS = 100000
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
ANONYMOUS_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def scope_statements(statements):
    """枚举当前词法层，统计定义/导入/写入；遇到子定义不进入其函数体。"""
    nodes, writes = [], {}
    pending = list(reversed(statements))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if isinstance(node, DEFINITIONS):
            writes.setdefault(node.name, []).append(node)
            continue
        if isinstance(node, ANONYMOUS_SCOPES):
            # s18 修改：匿名作用域有独立绑定，不将内部参数误连到外层同名对象。
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or (
                    alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                )
                writes.setdefault(name, []).append(node)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            writes.setdefault(node.id, []).append(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                writes.setdefault(name, []).append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            writes.setdefault(node.name, []).append(node)
        pending.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes, writes


# s18 新增：每条边都保存证据位置；未解析表达式没有虚构 target_id。
@dataclass(frozen=True)
class Relation:
    """描述同版源码的一条静态关系或未解析调用。

    输入：起点、可选终点、种类、表达式、位置和说明；输出：可存储/复核的事实。
    流程：RelationshipGraph.build 创建 → RelationshipIndex 保存 → related_code 导航。
    边界：描述静态绑定，不证明运行时一定执行；target_id=None 表示 unresolved。
    """

    source_id: str
    target_id: str | None
    kind: str
    path: str
    line: int
    expression: str
    reason: str = ""


class RelationshipGraph:
    """保存一个源码版本的节点、关系和可解析的名称表。

    输入：快照和实体目录；输出：图元数据，不保留第二份源码正文。
    流程：模块/实体节点 → 模块级定义表 → 分文件解析导入与作用域 → 生成有限边。
    边界：不执行目标代码，不连接图数据库；动态分派与重绑定保守记为 unresolved。
    """

    def __init__(
        self,
        *,
        fingerprint: str,
        nodes: dict[str, dict[str, Any]],
        relations: list[Relation],
        parse_errors: dict[str, str],
    ) -> None:
        """持有同代节点和事实，供持久化与单跳查询，不在构造时分析代码。"""
        self.fingerprint = fingerprint
        self.nodes = nodes
        self.relations = relations
        self.parse_errors = parse_errors
        # s18 新增：按端点分组；只保存边引用，不为每次导航遍历整张关系表。
        self.incoming: dict[str, list[Relation]] = {}
        self.outgoing: dict[str, list[Relation]] = {}
        for relation in relations:
            self.outgoing.setdefault(relation.source_id, []).append(relation)
            if relation.target_id is not None:
                self.incoming.setdefault(relation.target_id, []).append(relation)

    @classmethod
    def build(cls, snapshot: SourceSnapshot, catalog: EntityCatalog) -> RelationshipGraph:
        """从当前 Python 源码建立可解释的静态关系。

        输入：同一 fingerprint 的快照/目录；输出：calls/imports/inherits 图。
        流程：建立稳定节点身份 → 解析 AST → 收集模块定义 → 分析各作用域。
        边界：只支持仓库内导入；外部包、星号导入、动态属性和重绑定不猜目标。
        """
        if catalog.fingerprint != snapshot.fingerprint:
            raise ValueError("关系目录与源码版本不同")
        nodes = {entity.entity_id: asdict(entity) for entity in catalog.entities}
        trees: dict[str, ast.Module] = {}
        modules: dict[str, str] = {}
        module_ids: dict[str, str] = {}
        definitions = {
            (entity.path, entity.definition_line): entity.entity_id for entity in catalog.entities
        }
        exports: dict[str, dict[str, str | None]] = {}
        for path, data in snapshot.file_contents.items():
            if not path.endswith(".py") or path in catalog.parse_errors:
                continue
            tree = ast.parse(data.decode("utf-8-sig"), filename=path)
            trees[path] = tree
            name = path[:-3].replace("/", ".")
            name = name.removesuffix(".__init__")
            node_id = hashlib.sha256(
                f"module:{path}:{snapshot.file_hashes[path]}".encode()
            ).hexdigest()
            if name in modules:
                raise ValueError("模块路径存在歧义，不能选择其中一个作为导入目标")
            modules[name] = path
            module_ids[path] = node_id
            nodes[node_id] = {
                "entity_id": node_id,
                "path": path,
                "qualified_name": name,
                "name": name,
                "kind": "module",
                "start_line": 1,
                "end_line": max(1, len(source_lines(data))),
                "content_hash": snapshot.file_hashes[path],
            }
            top: dict[str, str | None] = {}
            _, module_writes = scope_statements(tree.body)
            for statement in tree.body:
                if isinstance(statement, DEFINITIONS):
                    top[statement.name] = (
                        definitions[(path, statement.lineno)]
                        if len(module_writes[statement.name]) == 1
                        else None
                    )
            exports[path] = top
        graph = cls(
            fingerprint=snapshot.fingerprint,
            nodes=nodes,
            relations=[],
            parse_errors=catalog.parse_errors,
        )
        # s18 修改：成员表全仓库只建一次；重复限定名保留歧义，不能让最后一个定义获胜。
        members: dict[tuple[str, str], str | None] = {}
        for key, node in nodes.items():
            if node["kind"] == "module":
                continue
            member = (node["path"], node["qualified_name"])
            members[member] = key if member not in members else None
        # s18 修改：方法被同一类中的赋值覆盖时，不再把旧定义当当前类成员。
        for path, tree in trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                class_name = nodes[definitions[(path, node.lineno)]]["qualified_name"]
                _, writes = scope_statements(node.body)
                for name, occurrences in writes.items():
                    if len(occurrences) != 1 or not isinstance(occurrences[0], DEFINITIONS):
                        members[(path, class_name + "." + name)] = None
        # s18 新增：先建立跨文件定义表，再分析调用；扫描顺序不会决定能否找到被调用者。
        for path, tree in trees.items():
            analyzer = _FileAnalyzer(
                graph=graph,
                path=path,
                modules=modules,
                module_ids=module_ids,
                definitions=definitions,
                exports=exports,
                members=members,
            )
            analyzer.analyze_scope(tree.body, module_ids[path], {})
        if len(graph.relations) > MAX_RELATIONS:
            raise ValueError("关系超过 100000 条，请缩小目标")
        # s18 修改：分析器先收集事实，最终图按完整边构造邻接索引；不返回空索引的构建容器。
        return cls(
            fingerprint=snapshot.fingerprint,
            nodes=nodes,
            relations=graph.relations,
            parse_errors=catalog.parse_errors,
        )

    def neighbors(
        self, entity_id: str, *, direction: str, kind: str, limit: int, scope: str = ""
    ) -> tuple[list[Relation], bool]:
        """在当前图中查询有界一跳，不递归展开邻居。

        输入：当前实体 ID、incoming/outgoing、关系种类、1–20 条上限。
        输出：关系列表与是否省略；未知终点仅可作为 outgoing 的 unresolved 返回。
        流程：校验 ID 和参数 → 按端点/种类筛选 → 证据位置排序 → 截取上限。
        """
        if entity_id not in self.nodes:
            raise ValueError("未知或过期 entity_id，请重新 search_code 获取当前实体")
        if direction not in {"incoming", "outgoing"} or kind not in {
            "calls",
            "imports",
            "inherits",
            "all",
        }:
            raise ValueError("direction 为 incoming/outgoing；kind 为 calls/imports/inherits/all")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit 必须为 1–20")
        adjacent = self.outgoing if direction == "outgoing" else self.incoming
        selected = [
            relation
            for relation in adjacent.get(entity_id, [])
            # s18 新增：s19 的范围内补充先过滤再限额，不能让范围外调用者耗尽名额。
            if (kind == "all" or relation.kind == kind)
            and (not scope or relation.path.startswith(scope + "/"))
        ]
        selected.sort(key=lambda edge: (edge.path, edge.line, edge.kind, edge.expression))
        return selected[:limit], len(selected) > limit


# s18 新增：底层解析器与对外编排分开；名称解析不泄漏到 agent_loop。
class _FileAnalyzer:
    """在一个文件内解析词法作用域，不推测动态类型。

    输入：同代图、AST、模块/定义/成员查找表；输出：追加带证据的 Relation。
    流程：analyze_scope 先找局部写入，再绑定确定名称，最后提取调用并进入子作用域。
    边界：条件赋值、参数、重绑定及动态返回值不作为确定别名。
    """

    def __init__(self, *, graph, path, modules, module_ids, definitions, exports, members):
        self.graph, self.path = graph, path
        self.modules, self.module_ids = modules, module_ids
        self.definitions, self.exports = definitions, exports
        self.members = members

    def analyze_scope(
        self,
        statements: list[ast.stmt],
        owner: str,
        parent: dict[str, str | None],
        parameters: list[str] | None = None,
    ) -> None:
        """分析一个作用域，子函数使用独立局部表。

        输入：语句、所属实体、外层绑定和参数；输出：向 graph.relations 追加事实。
        流程：收集写入/遮蔽 → 绑定直接定义与导入 → 按源码顺序绑定简单别名
        → 提取调用/继承 → 分析子类和子函数。未知名称保留 None，不向全仓库按同名兜底。
        """
        bindings = dict(parent)
        direct_statements = {id(statement) for statement in statements}
        nodes, writes = scope_statements(statements)
        for name in parameters or []:
            writes.setdefault(name, []).append(ast.Name(id=name))
        # s18 新增：所有局部名先遮蔽外层；x=x() 不会错误绑定到一个同名全局实体。
        bindings.update({name: None for name in writes})
        for name, occurrences in writes.items():
            if (
                len(occurrences) == 1
                and isinstance(occurrences[0], DEFINITIONS)
                and id(occurrences[0]) in direct_statements
            ):
                bindings[name] = self.definitions[(self.path, occurrences[0].lineno)]
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node, owner, bindings, writes, id(node) in direct_statements)
        # s18 新增：仅支持直接单目标别名/构造赋值；不做执行路径推测。
        assignments = [
            node
            for node in nodes
            if isinstance(node, ast.Assign)
            and id(node) in direct_statements
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]
        # s18 修改：只按源码顺序处理一次，避免反复传播掩盖前向引用，复杂别名宁可 unresolved。
        for assignment in assignments:
            name = assignment.targets[0].id
            if len(writes[name]) != 1:
                continue
            value = assignment.value
            target = self.resolve(value.func if isinstance(value, ast.Call) else value, bindings)
            if isinstance(value, ast.Call) and (
                target is None or self.graph.nodes[target]["kind"] != "class"
            ):
                target = None
            bindings[name] = target
        for node in nodes:
            if isinstance(node, ast.Call):
                target = self.resolve(node.func, bindings)
                self._edge(owner, target, "calls", node, ast.unparse(node.func))
            elif isinstance(node, ANONYMOUS_SCOPES):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        self._edge(owner, None, "calls", inner, ast.unparse(inner.func))
            elif isinstance(node, ast.ClassDef):
                child_id = self.definitions[(self.path, node.lineno)]
                for base in node.bases:
                    target = self.resolve(base, bindings)
                    if target and self.graph.nodes[target]["kind"] != "class":
                        target = None
                    self._edge(child_id, target, "inherits", base, ast.unparse(base))
                self.analyze_scope(node.body, child_id, bindings)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_id = self.definitions[(self.path, node.lineno)]
                parameters = [
                    item.arg
                    for item in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                ]
                if node.args.vararg:
                    parameters.append(node.args.vararg.arg)
                if node.args.kwarg:
                    parameters.append(node.args.kwarg.arg)
                # s18 新增：方法不捕获类的命名空间，避免把类属性当 Python 闭包变量。
                enclosing = parent if self.graph.nodes[owner]["kind"] == "class" else bindings
                self.analyze_scope(node.body, child_id, enclosing, parameters)

    def resolve(self, node: ast.AST, bindings: dict[str, str | None]) -> str | None:
        """仅解析名称、明确类成员及明确模块成员；动态返回值不被当作已知实例。"""
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.resolve(node.value, bindings)
            if parent is None:
                return None
            entity = self.graph.nodes[parent]
            if entity["kind"] == "module":
                return self.exports.get(entity["path"], {}).get(node.attr)
            if entity["kind"] == "class":
                return self.members.get(
                    (entity["path"], entity["qualified_name"] + "." + node.attr)
                )
        if isinstance(node, ast.Call):
            target = self.resolve(node.func, bindings)
            if target and self.graph.nodes[target]["kind"] == "class":
                return target
        return None

    def _import(self, node, owner, bindings, writes, direct):
        """绑定仓库内 import，外部包与重导出不猜测。"""
        package = list(PurePosixPath(self.path).parent.parts)
        for alias in node.names:
            if isinstance(node, ast.Import):
                name = alias.asname or alias.name.split(".")[0]
                module = alias.name if alias.asname else alias.name.split(".")[0]
                path = self.modules.get(module)
                target = self.module_ids.get(path)
            else:
                name = alias.asname or alias.name
                # s18 修改：越过包根的相对导入不能意外命中仓库根下同名文件。
                if node.level > len(package):
                    self._edge(owner, None, "imports", node, ast.unparse(node))
                    continue
                prefix = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join(prefix + ([node.module] if node.module else []))
                path = self.modules.get(module)
                target = self.exports.get(path, {}).get(alias.name)
                if target is None:
                    target = self.module_ids.get(self.modules.get(module + "." + alias.name))
            if direct and name != "*" and len(writes.get(name, [])) == 1:
                bindings[name] = target
            self._edge(owner, target, "imports", node, ast.unparse(node))

    def _edge(self, source, target, kind, node, expression):
        """限制事实总数；未知目标保留原表达式和明确原因。"""
        if len(self.graph.relations) >= MAX_RELATIONS:
            raise ValueError("关系超过 100000 条")
        self.graph.relations.append(
            Relation(
                source,
                target,
                kind,
                self.path,
                node.lineno,
                expression[:200],
                "" if target else "动态、外部或重绑定，无法唯一解析",
            )
        )
