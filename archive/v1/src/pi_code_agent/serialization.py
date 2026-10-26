"""来自 s05：保持 JSON 转换与会话文件选择；不导入教程或模型 SDK。"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Message = dict[str, Any]
SessionSaver = Callable[[Message], None]
DEFAULT_SESSION_ROOT = Path(".sessions/pi-code-agent")


def _to_jsonable(value: Any) -> Any:
    """把 SDK 对象递归转换为可写入 JSON 的基础值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return {
            str(key): _to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _create_new_session_path(root: Path) -> Path:
    """创建带时间戳且不覆盖已有文件的新会话路径。"""
    root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("session-%Y%m%d-%H%M%S")
    path = root / f"{stem}.jsonl"
    suffix = 2
    while path.exists():
        path = root / f"{stem}-{suffix}.jsonl"
        suffix += 1
    path.touch()
    return path


def parse_session_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """解析会话 Agent 共用的启动选项，不创建文件或模型客户端。

    输入：命令行参数，支持 --session 与 --mode default/read-only。
    输出：Namespace，session 是可选 Path，mode 是已校验的模式字符串。
    流程：注册两个选项 → argparse 校验 → 返回；无效选项或 --help 在这里退出。
    边界：只解析用户选择，工具筛选和权限 Hook 仍由各章 main 显式组装。
    """
    parser = argparse.ArgumentParser(description="运行会话 Agent")
    parser.add_argument("--mode", choices=("default", "read-only"), default="default")
    parser.add_argument("--session", type=Path, metavar="PATH", help="加载已有的 JSONL 会话文件")
    return parser.parse_args(arguments)


def select_session_path(
    history_path: Path | None,
    *,
    session_root: Path = DEFAULT_SESSION_ROOT,
) -> Path:
    """选择唯一会话文件，不改变会话格式或当前工具权限。

    输入：可选历史文件及本章默认目录；输出：存在的历史文件或新建的唯一 JSONL 路径。
    流程：没有历史路径时创建新文件；有路径时校验文件存在，原样返回。
    边界：只选择存储位置，不加载消息、不恢复授权，也不创建模型客户端。
    """
    if history_path is None:
        return _create_new_session_path(session_root)
    if not history_path.is_file():
        raise ValueError(f"会话文件不存在：{history_path}")
    return history_path


def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _text_from_content(content: Any) -> str:
    """提取模型响应中的文本块，用于终端展示。"""
    if isinstance(content, str):
        return content
    return "\n".join(
        str(text)
        for block in content
        if _get(block, "type") == "text"
        if (text := _get(block, "text"))
    )
