"""渠道风格的纯聚合器。

该模块不读写数据库；调用方必须先明确哪些候选已审核，才能将它们编译进快照。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


_STYLE_LIST_FIELDS = (
    "common_words",
    "emoji_habits",
    "special_expressions",
    "topics_of_interest",
)

_STYLE_TEXT_FIELDS = (
    "tone",
    "sentence_style",
    "punctuation_style",
    "addressing_style",
)


def merge_styles(styles: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """以确定性统计方式合并同一渠道的已审核风格观察。"""
    counters = {field: Counter() for field in _STYLE_LIST_FIELDS}
    text_values = {field: [] for field in _STYLE_TEXT_FIELDS}

    for style in styles:
        for field, counter in counters.items():
            values = style.get(field, [])
            if isinstance(values, list):
                counter.update(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
        for field, values in text_values.items():
            value = style.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())

    merged = {
        "common_words": _top_values(counters["common_words"], 12),
        "emoji_habits": _top_values(counters["emoji_habits"], 8),
        "special_expressions": _top_values(counters["special_expressions"], 6),
        "topics_of_interest": _top_values(counters["topics_of_interest"], 6),
    }
    merged.update({field: _majority(values) for field, values in text_values.items()})
    return merged


async def aggregate(styles: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """兼容旧调用方；不再隐式读取或改写任何运行时风格。"""
    return merge_styles(styles or [])


def _top_values(counter: Counter[str], limit: int) -> list[str]:
    return [value for value, _ in counter.most_common(limit)]


def _majority(values: list[str]) -> str:
    return Counter(values).most_common(1)[0][0] if values else ""
