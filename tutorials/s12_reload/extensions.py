# ===== s12 快照 =====
# 来自 s11：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s07：可信扩展工厂的发现、加载和工具注册，参考 Pi loader/runner。"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from importlib.abc import MetaPathFinder
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .tools import Tool, ToolResult


# ===== s07 新增 =====
# 来自 s07：保持；发现只返回路径，不执行项目代码；本地、全局、显式路径保持稳定顺序。
def discover_extensions(
    workspace: Path,
    *,
    global_dir: Path | None = None,
    paths: Sequence[Path] = (),
    project: bool = True,
) -> list[Path]:
    """发现根目录 py 和子目录 index.py，去重实际路径，不递归扫描业务项目。"""
    workspace = workspace.resolve()
    paths = [path if path.is_absolute() else workspace / path for path in paths]
    locations = []
    if project:
        locations.append(workspace / ".pi-code-agent" / "extensions")
    if global_dir is not None:
        locations.append(global_dir)
    locations.extend(paths)
    explicit = {path.resolve() for path in paths}
    found: list[Path] = []
    for location in locations:
        location = location.resolve()
        if not location.exists() and location not in explicit:
            continue
        if location.is_dir():
            if (location / "index.py").is_file():
                candidates = [location / "index.py"]
            else:
                candidates = []
                for child in sorted(location.iterdir()):
                    if child.is_file() and child.suffix == ".py" and not child.name.startswith("_"):
                        candidates.append(child)
                    elif child.is_dir() and (child / "index.py").is_file():
                        candidates.append(child / "index.py")
        else:
            candidates = [location]
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate not in found:
                found.append(candidate)
    return found


# s12 新增：只拦截本运行时唯一命名空间，依赖模块也读取新源码，避免同秒同长度 pyc。
class FreshSourceLoader(SourceFileLoader):
    """保留原生 ModuleSpec/资源读取，只绕开所属 Python 源文件的字节码缓存。"""

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)


class ExtensionModuleFinder(MetaPathFinder):
    """输入唯一包前缀和扩展目录，输出自有子模块的源码加载器，不改变全局 sys.path。"""

    def __init__(self, namespace, root):
        self.namespace, self.root = namespace, root

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.namespace + "."):
            return None
        relative = fullname[len(self.namespace) + 1 :].split(".")
        base = self.root.joinpath(*relative)
        if (base / "__init__.py").is_file():
            source, locations = base / "__init__.py", [str(base)]
        elif base.with_suffix(".py").is_file():
            source, locations = base.with_suffix(".py"), None
        elif base.is_dir():
            # s12 新增：无 __init__ 的命名空间包也允许继续发现子模块。
            from importlib.machinery import ModuleSpec

            spec = ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(base)]
            return spec
        else:
            return None
        loader = FreshSourceLoader(fullname, str(source))
        return spec_from_file_location(
            fullname, source, loader=loader, submodule_search_locations=locations
        )


class ExtensionContext:
    """带运行时代次检查的会话视图，避免重载后逃逸处理函数继续操作新会话。

    输入：所属运行时与当前会话；输出：实时属性和受保护方法。
    流程：每次取值/调用先检查代次 → 交给会话；异步方法在实际 await 时再检查。
    边界：这是生命周期约束，不是 Python 权限沙箱；session 属性返回本视图。
    """

    def __init__(self, runtime, session):
        self._runtime, self._session = runtime, session

    def _validate(self):
        if self._runtime._closed or self._session.extensions is not self._runtime:
            raise RuntimeError("扩展上下文已失效，请在新代次事件中重新获取")

    @property
    def session(self):
        self._validate()
        return self

    @property
    def ui(self):
        self._validate()
        return SimpleNamespace(notify=self.notify)

    def notify(self, text):
        self._validate()
        return self._session._notify(text)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._validate()
        value = getattr(self._session, name)
        if inspect.iscoroutinefunction(value):

            async def invoke(*args, **kwargs):
                self._validate()
                return await value(*args, **kwargs)

            return invoke
        if callable(value):

            def invoke(*args, **kwargs):
                self._validate()
                return value(*args, **kwargs)

            return invoke
        return value


class ExtensionAPI:
    """提供一次工厂的注册事务，不让失败工厂留下半个工具集合。

    输入：Tool 声明；输出：工厂成功后可用的工具。
    流程：loading 收集 → active 提交；失败时清空、失效。
    边界：s12 允许活跃期动态注册，关闭/失败后禁止。原生 Python 扩展不是沙箱。
    """

    # 来自 s07：保持；工厂可使用当前宿主的类型，独立教学快照不必导入正式包。
    Tool = Tool
    ToolResult = ToolResult

    def __init__(self, source: str, runtime=None) -> None:
        self.source = source
        self._runtime = runtime
        self._commands = {}
        self._flags = {}
        self._state = "loading"
        self._tools: dict[str, Tool] = {}
        self._handlers: dict[str, list[Callable]] = {}

    # s12 新增：注册可以在 active 期间发生，工具选择仍在下一次请求生效。
    def set_active_tools(self, names):
        """切换已注册工具的可见集合，未知名称按 Pi 忽略。"""
        if self._state != "active" or self._runtime._session is None:
            raise RuntimeError("扩展尚未绑定活跃会话")
        ExtensionContext(self._runtime, self._runtime._session).set_active_tools(names)

    def get_active_tools(self):
        """读取当前启用名称，供扩展决定工具切换。"""
        if self._state != "active" or self._runtime._session is None:
            raise RuntimeError("扩展尚未绑定活跃会话")
        return ExtensionContext(self._runtime, self._runtime._session).get_active_tools()

    # 来自 s09：保持；命令与参数与工具分别注册，不占模型工具列表。
    def register_command(self, name: str, handler: Callable, *, description: str = ""):
        """注册 /name 命令，处理函数接收原始参数文本与命令上下文。"""
        # s12 修改：允许活跃期注册，失效代次仍拒绝，变化在下一请求取快照。
        if self._state not in {"loading", "active"}:
            raise RuntimeError("请在 setup 中注册命令")
        if (
            not name
            or not name.replace("-", "").replace("_", "").isalnum()
            or name in {"help", "reload", "quit"}
            or not callable(handler)
        ):
            raise ValueError("无效或保留的命令名称")
        self._commands[name] = (handler, description)

    def register_flag(self, name: str, *, type: str = "boolean", default=None, description=""):
        """声明 boolean/string 参数，默认值在工厂事务内，不污染失败后的运行时。"""
        # s12 修改：允许活跃期注册，失效代次仍拒绝，变化在下一请求取快照。
        if self._state not in {"loading", "active"}:
            raise RuntimeError("请在 setup 中注册参数")
        expected = {"boolean": bool, "string": str}.get(type)
        if (
            not name
            or not name.replace("-", "").isalnum()
            or expected is None
            or (default is not None and default.__class__ is not expected)
        ):
            raise ValueError("参数声明或默认值错误")
        self._flags[name] = {"type": expected, "default": default, "description": description}

    def get_flag(self, name):
        """仅查询本扩展已声明的参数；CLI 在工厂完成后应用实际值。"""
        # s12 修改：允许活跃期注册，失效代次仍拒绝，变化在下一请求取快照。
        if self._state not in {"loading", "active"}:
            raise RuntimeError("扩展已失效")
        if name not in self._flags:
            return None
        return self._runtime.flag_values.get(name, self._flags[name]["default"])

    # 来自 s08：保持；声明拦截点；工厂失败时与工具一起丢弃，不提前执行。
    def on(self, event: str, handler: Callable) -> None:
        """注册同步或异步 Hook，输入事件与实时会话上下文，返回对应修改。"""
        # s12 修改：允许活跃期注册，失效代次仍拒绝，变化在下一请求取快照。
        if self._state not in {"loading", "active"}:
            raise RuntimeError("请在 setup 中注册 Hook")
        if event not in {
            "session_start",
            "session_shutdown",
            "input",
            "context",
            "tool_call",
            "tool_result",
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_end",
        }:
            raise ValueError("不支持的 Hook：" + event)
        if not callable(handler):
            raise TypeError("Hook 必须可调用")
        self._handlers.setdefault(event, []).append(handler)

    def register_tool(self, tool: Tool) -> None:
        """绑定声明与处理函数；同一工厂内同名工具替换前一项，保留 Pi 语义。"""
        # s12 修改：允许活跃期注册，失效代次仍拒绝，变化在下一请求取快照。
        if self._state not in {"loading", "active"}:
            raise RuntimeError("扩展已失效，不能注册工具")
        if not isinstance(tool, Tool):
            raise TypeError("register_tool 需要本运行时的 Tool")
        if tool.definition().parameters.get("type") != "object":
            raise ValueError("工具参数必须声明为对象 schema")

        async def execute(arguments):
            # 来自 s07：保持；关闭或失败后的旧处理函数不再产生工具副作用。
            if self._state != "active":
                raise RuntimeError("扩展已失效")
            return await tool.execute(arguments)

        self._tools[tool.name] = replace(tool, execute=execute)


def _unload_namespace(namespace: str) -> None:
    """仅移除本次加载拥有的模块命名空间，不清理其他插件或全局模块。"""
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(namespace + "."):
            sys.modules.pop(name, None)


class ExtensionRuntime:
    """持有已成功初始化的扩展、诊断和模块生命周期。

    输入：可信文件或内联工厂；输出：按 Pi 优先级合并的工具与来源信息。
    流程：发现 → 独立模块 → setup(api) → 成功提交 → 会话使用 → close 清理。
    边界：普通加载错误隔离，取消向外传播；不下载、安装或隔离 Python 权限。
    """

    def __init__(self) -> None:
        self._extensions: list[ExtensionAPI] = []
        self.diagnostics: list[dict[str, str]] = []
        self._namespaces: list[str] = []
        self._paths: set[Path] = set()
        self._closed = False
        # s12 新增：绑定、来源重放和自有模块加载器共同管理可失效的代次。
        self._session = None
        self._factories = []
        self._replay = []
        self._requested_paths = []
        self._finders = []
        self.discover = None
        self.flag_values = {}

    @property
    def extensions(self) -> tuple[ExtensionAPI, ...]:
        """返回有序扩展集合，不允许外部移除仍由本运行时负责关闭的实例。"""
        return tuple(self._extensions)

    async def register(self, factory: Callable, *, source: str = "<inline>") -> ExtensionAPI:
        """运行同步或异步工厂；成功才提交注册，异常时使逃逸 API 也失效。"""
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        if not callable(factory):
            raise TypeError("扩展必须提供 setup(api) 工厂")
        api = ExtensionAPI(source, self)
        try:
            result = factory(api)
            if inspect.isawaitable(result):
                await result
        except BaseException:
            api._state = "failed"
            api._tools.clear()
            raise
        api._state = "active"
        self._extensions.append(api)
        self._factories.append((factory, source))
        self._replay.append(("factory", (factory, source)))
        return api

    async def load(self, paths: Sequence[Path]) -> None:
        """加载明确选择的可信文件，支持包内相对导入；失败不污染后续工厂。"""
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        for path in paths:
            path = path.resolve()
            if path not in self._requested_paths:
                self._requested_paths.append(path)
                self._replay.append(("path", path))
            if path in self._paths:
                continue
            namespace = "_pi_code_agent_extension_" + uuid4().hex
            finder = ExtensionModuleFinder(namespace, path.parent)
            sys.meta_path.insert(0, finder)
            try:
                if not path.is_file() or path.suffix != ".py":
                    raise ValueError("扩展入口必须是已有 Python 文件")
                # s07 修复：原生 ModuleSpec 提供完整包语义，支持资源读取与模块内省。
                spec = spec_from_file_location(
                    namespace, path, submodule_search_locations=[str(path.parent)]
                )
                if spec is None or spec.loader is None:
                    raise ImportError("无法构建扩展模块定义")
                module = module_from_spec(spec)
                sys.modules[namespace] = module
                # s07 调整：Pi 使用 jiti；Python 显式编译源码，入口不受旧 pyc 缓存影响。
                exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)  # noqa: S102 - 显式可信扩展加载，不提供沙箱
                await self.register(getattr(module, "setup", None), source=str(path))
                self._replay.pop()  # s12 修复：文件来源留在原位置，工厂仅按路径重放。
                self._factories.pop()  # s12：文件工厂按路径重载，不重复当作内联工厂。
            except BaseException as error:
                sys.meta_path.remove(finder)
                _unload_namespace(namespace)
                if not isinstance(error, Exception):
                    raise
                self.diagnostics.append({"source": str(path), "error": str(error)})
                continue
            self._finders.append(finder)
            self._namespaces.append(namespace)
            self._paths.add(path)

    def resolve_tools(
        self, builtin_tools: Sequence[Tool] = (), *, sdk_tools: Sequence[Tool] = ()
    ) -> list[Tool]:
        """内置 → 扩展首个同名 → SDK 显式工具；同一扩展内取最后注册。"""
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        catalog = {tool.name: tool for tool in builtin_tools}
        seen = set()
        for extension in self.extensions:
            for name, tool in extension._tools.items():
                if name not in seen:
                    catalog[name] = tool
                    seen.add(name)
        for tool in sdk_tools:
            catalog[tool.name] = tool
        return list(catalog.values())

    # 来自 s09：保持；跨扩展同名声明首个优先，与工具注册来源顺序一致。
    def commands(self):
        """返回实际可调用命令表。"""
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        values = {}
        for extension in self.extensions:
            for name, command in extension._commands.items():
                values.setdefault(name, command)
        return values

    def parse_flags(self, arguments):
        """严格解析扩展声明过的 --flag、--no-flag、--name=value；未知项报错。"""
        declarations = {}
        for extension in self.extensions:
            for name, flag in extension._flags.items():
                declarations.setdefault(name, flag)
        values = {}
        index = 0
        while index < len(arguments):
            token = arguments[index]
            if not token.startswith("--"):
                raise ValueError("无效扩展参数：" + token)
            name, separator, value = token[2:].partition("=")
            negative = name.startswith("no-") and name[3:] in declarations
            if negative:
                name = name[3:]
            flag = declarations.get(name)
            if flag is None:
                raise ValueError("未知参数：" + token)
            if flag["type"] is bool:
                if separator and value not in {"true", "false"}:
                    raise ValueError("布尔参数只能为 true/false")
                values[name] = not negative if not separator else value == "true"
            else:
                if negative:
                    raise ValueError("字符串参数不能使用 no- 前缀")
                if not separator:
                    index += 1
                    if index >= len(arguments) or arguments[index].startswith("--"):
                        raise ValueError("字符串参数缺少值")
                    value = arguments[index]
                values[name] = value
            index += 1
        self.flag_values = values
        return deepcopy(values)

    def tool_sources(self) -> dict[str, str]:
        """返回扩展工具最终来源，不把诊断或路径写入模型上下文。"""
        sources = {}
        for extension in self.extensions:
            for name in extension._tools:
                sources.setdefault(name, extension.source)
        return sources

    # 来自 s08：保持；按扩展/注册顺序执行；工具调用异常向外传播以阻止副作用。
    async def dispatch(self, event: dict, session) -> dict:
        """输入事件副本，输出链式修改；普通观察失败记录诊断，原生取消传播。"""
        # s08 修复：已关闭运行时不能通过逃逸会话重新触发 Hook 副作用。
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        current = deepcopy(event)
        kind = event["type"]
        context = ExtensionContext(self, session)
        for extension in self.extensions:
            for handler in tuple(extension._handlers.get(kind, ())):
                try:
                    result = handler(deepcopy(current), context)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is None:
                        continue
                    if not isinstance(result, dict):
                        raise TypeError("Hook 返回值必须是字典或 None")
                    candidate = deepcopy(current)
                    if kind == "input":
                        action = result.get("action", "continue")
                        if action not in {"continue", "transform", "handled"}:
                            raise ValueError("无效 input action")
                        if action == "transform":
                            if not isinstance(result.get("text"), str):
                                raise TypeError("transform 需要文本")
                            candidate["text"] = result["text"]
                        if action == "handled":
                            candidate["action"] = "handled"
                            return candidate
                    elif kind == "context" and "messages" in result:
                        # 来自 s08：保持；使用同一消息编解码器检查修改，临时上下文不落盘。
                        from .session import encode_message

                        values = list(result["messages"])
                        for message in values:
                            encode_message(message)
                        candidate["messages"] = values
                    elif kind == "tool_call" and result.get("block"):
                        return {**candidate, **result}
                    elif kind == "tool_result":
                        value = ToolResult(
                            result.get("content", current["content"]),
                            result.get("is_error", current["is_error"]),
                            result.get("terminate", current["terminate"]),
                        )
                        candidate.update(
                            content=value.content,
                            is_error=value.is_error,
                            terminate=value.terminate,
                        )
                    current = deepcopy(candidate)
                except Exception as error:
                    if kind == "tool_call":
                        raise
                    self.diagnostics.append(
                        {"source": extension.source, "event": kind, "error": str(error)}
                    )
        return current

    def close(self) -> None:
        """宿主空闲后释放所属模块并使旧 API 失效，可重复调用。"""
        for extension in self.extensions:
            extension._state = "closed"
        for namespace in self._namespaces:
            _unload_namespace(namespace)
        self._namespaces.clear()
        for finder in self._finders:
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
        self._finders.clear()
        self._closed = True
