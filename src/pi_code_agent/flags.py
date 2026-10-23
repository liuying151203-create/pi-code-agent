"""s19：移植 Pi 的扩展参数声明与取值，来源和许可见 docs/third-party-notices.md。"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ExtensionFlag:
    """保存扩展参数声明，供帮助、解析和 SDK 校验共用。"""

    name: str
    type: Literal["boolean", "string"]
    description: str
    default: bool | str | None = None


# ===== s19 新增：声明与取值分离，扩展只加载一次 =====
class FlagRegistry:
    """管理扩展启动参数，不读取环境或创建模型。

    输入：boolean/string 声明、CLI 或 SDK 的显式值；输出：校验后的取值及参数帮助。
    流程：register → CLI 增加选项并解析 / SDK 设置值 → validate → get。
    边界：不支持热更新；完成启动后拒绝新增声明，重名及内置选项冲突明确失败。
    """

    def __init__(self) -> None:
        self.definitions: dict[str, ExtensionFlag] = {}
        self.values: dict[str, bool | str] = {}
        self.frozen = False

    def register(
        self,
        name: str,
        *,
        type: Literal["boolean", "string"],
        description: str,
        default: bool | str | None = None,
    ) -> None:
        """校验名称、类型、默认值和重名后记录声明，不消费命令行。"""
        if self.frozen:
            raise RuntimeError("扩展启动参数已固定，不能继续注册")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("扩展参数名称必须为小写字母开头的标识符，不带 --")
        if type not in {"boolean", "string"}:
            raise ValueError("扩展参数类型只支持 boolean 或 string")
        if name in self.definitions:
            raise ValueError(f"扩展参数名称重复：--{name}")
        # s19 新增：CLI 与 SDK 使用同一命名约束，自动反向选项也不能覆盖其他声明。
        if name in {"help", "workspace", "data-dir", "session", "mode", "extension"}:
            raise ValueError(f"扩展参数与内置选项冲突：--{name}")
        names = {name, f"no-{name}"} if type == "boolean" else {name}
        for existing in self.definitions.values():
            taken = (
                {existing.name, f"no-{existing.name}"}
                if existing.type == "boolean"
                else {existing.name}
            )
            if names & taken:
                raise ValueError(f"扩展参数选项冲突：--{name}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("扩展参数必须有帮助说明")
        expected = bool if type == "boolean" else str
        if default is not None and not isinstance(default, expected):
            raise ValueError(f"扩展参数 --{name} 的默认值类型应为 {type}")
        if name in self.values and not isinstance(self.values[name], expected):
            raise ValueError(f"扩展参数 --{name} 的值类型应为 {type}")
        self.definitions[name] = ExtensionFlag(name, type, description.strip(), default)

    def set_values(self, values: Mapping[str, bool | str]) -> None:
        """保存显式值，SDK 可在注册前提供；已声明参数立即校验类型。

        输入：名称到 bool/string 的映射；输出：无。
        流程：完整校验副本 → 校验已声明类型 → 替换值；未知名称在启动完成时统一拒绝。
        """
        if self.frozen:
            raise RuntimeError("扩展启动参数已固定")
        candidate = dict(values)
        for name, value in candidate.items():
            if not isinstance(name, str) or not isinstance(value, (bool, str)):
                raise TypeError("扩展参数值必须是名称到 bool/string 的映射")
            definition = self.definitions.get(name)
            if definition is not None:
                expected = bool if definition.type == "boolean" else str
                if not isinstance(value, expected):
                    raise ValueError(f"扩展参数 --{name} 的值类型应为 {definition.type}")
        self.values = candidate

    def validate(self) -> None:
        """注册完成后拒绝未声明的 SDK 参数，不静默忽略拼写错误。"""
        unknown = set(self.values) - self.definitions.keys()
        if unknown:
            raise ValueError(f"未知扩展参数：{', '.join(sorted(unknown))}")

    def get(self, name: str) -> bool | str | None:
        """返回显式值或默认值；未注册时与 Pi 的 undefined 对应为 None。"""
        definition = self.definitions.get(name)
        return None if definition is None else self.values.get(name, definition.default)

    def add_to_parser(self, parser: argparse.ArgumentParser) -> None:
        """把声明接入现有 argparse，预先拒绝与内置或自动反向选项的冲突。

        输入：已有内置选项的 parser；输出：无，加入扩展参数帮助组。
        流程：收集真实选项名 → 校验所有新选项 → 按声明加入 string/boolean 参数。
        """
        taken = set(parser._option_string_actions)
        for flag in self.definitions.values():
            names = [f"--{flag.name}"]
            if flag.type == "boolean":
                names.append(f"--no-{flag.name}")
            if any(name in taken for name in names):
                raise ValueError(f"扩展参数选项冲突：--{flag.name}")
            taken.update(names)
        group = parser.add_argument_group("扩展启动参数")
        for flag in self.definitions.values():
            options = {
                "dest": f"extension:{flag.name}",
                "default": argparse.SUPPRESS,
                "help": f"{flag.description}（默认：{flag.default!r}）",
            }
            if flag.type == "boolean":
                group.add_argument(
                    f"--{flag.name}", action=argparse.BooleanOptionalAction, **options
                )
            else:
                group.add_argument(f"--{flag.name}", metavar="VALUE", **options)

    def apply_namespace(self, namespace: argparse.Namespace) -> None:
        """把 argparse 的显式扩展值接到同一 SDK 校验路径，省略值保留声明默认值。"""
        parsed = vars(namespace)
        self.set_values(
            {
                name: parsed[f"extension:{name}"]
                for name in self.definitions
                if f"extension:{name}" in parsed
            }
        )
        self.validate()
