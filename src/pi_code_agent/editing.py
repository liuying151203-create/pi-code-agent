"""s02：按原文件定位整批替换，通过全部检查后写入一次。

移植自 Pi e4c75a732 的 agent/src/harness/tools/edit-diff.ts 与 edit.ts。
保留先精确后规范化匹配、唯一性、非重叠检查和未编辑行；Python 使用同步文件 I/O。
完整 MIT 许可见 docs/third-party-notices.md。
"""

# Copyright (c) 2025 Mario Zechner
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
import unicodedata
from itertools import pairwise
from pathlib import Path


# s02 新增：对应 Pi normalizeForFuzzyMatch，仅用于查找，未涉及的行仍保留原文。
def _fuzzy(text: str) -> str:
    """统一 Unicode、行尾空白、引号、破折号和特殊空格以匹配文本。"""
    text = "\n".join(line.rstrip() for line in unicodedata.normalize("NFKC", text).split("\n"))
    text = re.sub("[\u2018\u2019\u201a\u201b]", "'", text)
    text = re.sub("[\u201c\u201d\u201e\u201f]", '"', text)
    text = re.sub("[\u2010-\u2015\u2212]", "-", text)
    return re.sub("[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", text)


# s02 新增：匹配位置统一使用 LF，写回格式由 edit_text_file 保留。
def _lf(text: str) -> str:
    """统一到 LF 进行定位，写回时恢复原文件的换行格式。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# s02 新增：只按替换实际涉及的行重建，防止模糊匹配改写其他行的空格和 Unicode。
def _replace_lines(original: str, base: str, matches: list[tuple[int, int, str]]) -> str:
    """把规范化空间中的替换映射到原始行，未触及的行直接复制。"""
    original_lines = re.findall(r"[^\n]*\n|[^\n]+", original)
    base_lines = re.findall(r"[^\n]*\n|[^\n]+", base)
    if len(original_lines) != len(base_lines):
        raise ValueError("规范化改变了行数，无法保留未编辑的行")
    offsets = [0]
    for line in base_lines:
        offsets.append(offsets[-1] + len(line))
    groups: list[tuple[int, int, list[tuple[int, int, str]]]] = []
    for start, end, replacement in matches:
        first = next(i for i in range(len(base_lines)) if offsets[i] <= start < offsets[i + 1])
        last = next(i + 1 for i in range(first, len(base_lines)) if offsets[i + 1] >= end)
        if groups and first < groups[-1][1]:
            previous_first, previous_last, previous_matches = groups[-1]
            previous_matches.append((start, end, replacement))
            groups[-1] = (previous_first, max(previous_last, last), previous_matches)
        else:
            groups.append((first, last, [(start, end, replacement)]))
    output = []
    next_line = 0
    for first, last, replacements in groups:
        output.extend(original_lines[next_line:first])
        changed = base[offsets[first] : offsets[last]]
        for start, end, replacement in reversed(replacements):
            changed = (
                changed[: start - offsets[first]] + replacement + changed[end - offsets[first] :]
            )
        output.append(changed)
        next_line = last
    output.extend(original_lines[next_line:])
    return "".join(output)


# s02 新增：教程和正式 SDK 共用编辑算法；调用方负责工作区边界及写入权限。
def edit_text_file(
    path: Path,
    old_text: str | None = None,
    new_text: str | None = None,
    *,
    edits: list[dict[str, str]] | None = None,
) -> None:
    """把所有替换定位在原文件上，校验后一次写回，不逐项写入。

    输入：已校验路径、Pi 风格 edits 列表，或兼容旧调用的 old_text/new_text。
    输出：无；失败抛出 ValueError/OSError，且校验失败不会改文件。
    流程：检查参数 → 保留 BOM/换行 → 原文定位 → 唯一及非重叠检查 → 写入。
    边界：同步单次操作，不提供并发写锁、崩溃恢复或 diff 展示。
    """
    if edits is not None and not isinstance(edits, list):
        raise ValueError("edits 必须是数组")
    replacements = list(edits) if edits is not None else []
    if old_text is not None or new_text is not None:
        replacements.append({"oldText": old_text, "newText": new_text})
    if not replacements:
        raise ValueError("edits 至少需要一项替换")
    normalized = []
    for edit in replacements:
        if (
            not isinstance(edit, dict)
            or set(edit) != {"oldText", "newText"}
            or not isinstance(edit["oldText"], str)
            or not isinstance(edit["newText"], str)
        ):
            raise ValueError("每项替换必须包含字符串 oldText 和 newText")
        old, new = _lf(edit["oldText"]), _lf(edit["newText"])
        if not old or not _fuzzy(old):
            raise ValueError("oldText 不能为空或仅包含行尾空白")
        normalized.append((old, new))

    # s02 修改：字节读取避免 Python 自动把 CRLF 转成 LF，保留 BOM 后再计算位置。
    raw = path.read_bytes().decode("utf-8")
    bom = "\ufeff" if raw.startswith("\ufeff") else ""
    content = raw[len(bom) :]
    ending = (
        "\r\n"
        if "\n" in content and content[content.index("\n") - 1 : content.index("\n")] == "\r"
        else "\n"
    )
    original = _lf(content)
    fuzzy = any(old not in original and _fuzzy(old) in _fuzzy(original) for old, _ in normalized)
    base = _fuzzy(original) if fuzzy else original
    matches = []
    for old, new in normalized:
        needle = old if old in base else _fuzzy(old)
        start = base.find(needle)
        if start < 0:
            raise ValueError(f"text not found in {path}")
        if _fuzzy(base).count(_fuzzy(old)) > 1:
            raise ValueError("替换文本必须唯一，请提供更多上下文")
        matches.append((start, start + len(needle), new))
    matches.sort(key=lambda match: match[0])
    if any(left[1] > right[0] for left, right in pairwise(matches)):
        raise ValueError("替换范围重叠，请合并为一项替换")

    updated = _replace_lines(original, base, matches)
    if updated == original:
        raise ValueError("替换没有产生变化")
    path.write_bytes((bom + updated.replace("\n", ending)).encode("utf-8"))
