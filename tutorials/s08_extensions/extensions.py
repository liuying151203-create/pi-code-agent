"""s08：可信扩展工厂的发现、加载和工具注册，参考 Pi loader/runner。"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

from .tools import Tool, ToolResult


# ===== s08 新增 =====
# s08 新增：发现只返回路径，不执行项目代码；本地、全局、显式路径保持稳定顺序。
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


class ExtensionAPI:
    """提供一次工厂的注册事务，不让失败工厂留下半个工具集合。

    输入：Tool 声明；输出：工厂成功后可用的工具。
    流程：loading 收集 → active 提交；失败时清空、失效。
    边界：本章只在工厂期间注册；动态注册在 s13 实现。原生 Python 扩展不是沙箱。
    """

    # s08 新增：工厂可使用当前宿主的类型，独立教学快照不必导入正式包。
    Tool = Tool
    ToolResult = ToolResult

    def __init__(self, source: str) -> None:
        self.source = source
        self._state = "loading"
        self._tools: dict[str, Tool] = {}

    def register_tool(self, tool: Tool) -> None:
        """绑定声明与处理函数；同一工厂内同名工具替换前一项，保留 Pi 语义。"""
        if self._state != "loading":
            raise RuntimeError("本章只允许在 setup 工厂中注册工具")
        if not isinstance(tool, Tool):
            raise TypeError("register_tool 需要本运行时的 Tool")
        if tool.definition().parameters.get("type") != "object":
            raise ValueError("工具参数必须声明为对象 schema")

        async def execute(arguments):
            # s08 新增：关闭或失败后的旧处理函数不再产生工具副作用。
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
        api = ExtensionAPI(source)
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
        return api

    async def load(self, paths: Sequence[Path]) -> None:
        """加载明确选择的可信文件，支持包内相对导入；失败不污染后续工厂。"""
        if self._closed:
            raise RuntimeError("扩展运行时已关闭")
        for path in paths:
            path = path.resolve()
            if path in self._paths:
                continue
            namespace = "_pi_code_agent_extension_" + uuid4().hex
            try:
                if not path.is_file() or path.suffix != ".py":
                    raise ValueError("扩展入口必须是已有 Python 文件")
                # s08 修复：原生 ModuleSpec 提供完整包语义，支持资源读取与模块内省。
                spec = spec_from_file_location(
                    namespace, path, submodule_search_locations=[str(path.parent)]
                )
                if spec is None or spec.loader is None:
                    raise ImportError("无法构建扩展模块定义")
                module = module_from_spec(spec)
                sys.modules[namespace] = module
                # s08 调整：Pi 使用 jiti；Python 显式编译源码，入口不受旧 pyc 缓存影响。
                exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)  # noqa: S102 - 显式可信扩展加载，不提供沙箱
                await self.register(getattr(module, "setup", None), source=str(path))
            except BaseException as error:
                _unload_namespace(namespace)
                if not isinstance(error, Exception):
                    raise
                self.diagnostics.append({"source": str(path), "error": str(error)})
                continue
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

    def tool_sources(self) -> dict[str, str]:
        """返回扩展工具最终来源，不把诊断或路径写入模型上下文。"""
        sources = {}
        for extension in self.extensions:
            for name in extension._tools:
                sources.setdefault(name, extension.source)
        return sources

    def close(self) -> None:
        """宿主空闲后释放所属模块并使旧 API 失效，可重复调用。"""
        for extension in self.extensions:
            extension._state = "closed"
        for namespace in self._namespaces:
            _unload_namespace(namespace)
        self._namespaces.clear()
        self._closed = True
