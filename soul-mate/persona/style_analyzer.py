"""素材分析器：将原始内容转为待审核的风格观察与角色候选结论。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from api import llm_client


_SOURCE_ALIASES = {
    "douyin": "douyin_reference",
    "wechat": "chat",
}
_SUPPORTED_SOURCE_TYPES = frozenset({
    "canonical_setup",
    "chat",
    "moments",
    "douyin_reference",
    "shared_history",
    "manual",
    "legacy",
})
_SOURCE_CHANNELS = {
    "chat": "wechat_chat",
    "moments": "moments",
    "douyin_reference": "reference",
}
_FACETS = frozenset({
    "identity",
    "value",
    "preference",
    "boundary",
    "habit",
    "communication",
    "life_context",
})
_POLARITIES = frozenset({"supports", "contradicts", "uncertain"})
_STYLE_LIST_FIELDS = (
    "common_words",
    "emoji_habits",
    "special_expressions",
    "topics_of_interest",
)
_STYLE_TEXT_FIELDS = ("sentence_style", "tone")


def normalize_source_type(source_type: str) -> str:
    normalized = _SOURCE_ALIASES.get(source_type.strip().lower(), source_type.strip().lower())
    if normalized not in _SUPPORTED_SOURCE_TYPES:
        raise ValueError(f"不支持的素材来源：{source_type}")
    return normalized


def channel_for_source(source_type: str) -> str | None:
    return _SOURCE_CHANNELS.get(normalize_source_type(source_type))


async def analyze_style(text: str, source_type: str) -> dict[str, Any]:
    """提取表达观察，不把任何观察直接写入运行时风格。"""
    normalized_source = normalize_source_type(source_type)
    channel = channel_for_source(normalized_source)
    if not channel:
        return {}

    result = await llm_client.lightweight_chat(
        f"""你在分析虚构角色的原始素材。素材来源为 {normalized_source}，其表达只适用于 {channel} 场景。
只返回 JSON 对象，不要输出解释：
{{
  "common_words": ["可能重复出现的词或短语"],
  "sentence_style": "句子长度、断句和标点习惯",
  "emoji_habits": ["表情或颜文字"],
  "tone": "语气描述",
  "special_expressions": ["有证据支撑的特色表达"],
  "topics_of_interest": ["表达中反复出现的话题"]
}}
约束：
- 仅描述素材中可观察到的表达，不补充事实或人格。
- 抖音参考素材只能用于人物理解，不能推导微信聊天口头禅。
- 无证据的字段用空数组或空字符串。

[素材开始]
{text[:4000]}
[素材结束]""",
        max_tokens=400,
    )
    return _normalize_style(_parse_object(result))


async def extract_persona_claims(text: str, source_type: str) -> list[dict[str, Any]]:
    """提取可审核的人格候选；不处理另一位聊天参与者的用户画像。"""
    normalized_source = normalize_source_type(source_type)
    result = await llm_client.lightweight_chat(
        f"""你在从虚构角色的原始素材中提取候选结论。素材来源为 {normalized_source}。
只返回 JSON 对象：
{{"claims":[{{
  "facet":"identity|value|preference|boundary|habit|communication|life_context",
  "claim_key":"稳定、简短的字段名",
  "claim_value":"候选结论",
  "context":{{"适用条件":"可选"}},
  "polarity":"supports|contradicts|uncertain",
  "confidence":0.0,
  "quote":"能支持该结论的原文短引"
}}]}}
约束：
- 所有结果都是待审核候选，不得将推测写成事实。
- 只描述目标角色；不要提取另一位聊天参与者的用户信息。
- 没有明确线索时返回空数组。
- 不要从抖音参考素材推导微信聊天表达；它只可产生人物理解类候选。

[素材开始]
{text[:4000]}
[素材结束]""",
        max_tokens=700,
    )
    raw_claims = _parse_object(result).get("claims", [])
    return _normalize_claims(raw_claims, normalized_source)


async def extract_life_facts(text: str, source_type: str) -> dict[str, Any]:
    """兼容旧调用方；新链路应使用 ``extract_persona_claims``。"""
    return {"claims": await extract_persona_claims(text, source_type)}


def _parse_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    payload = value.strip()
    if payload.startswith("```"):
        payload = payload.split("\n", 1)[1] if "\n" in payload else ""
        if payload.rstrip().endswith("```"):
            payload = payload.rstrip()[:-3]
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_style(raw: Mapping[str, Any]) -> dict[str, Any]:
    style: dict[str, Any] = {}
    for field in _STYLE_LIST_FIELDS:
        values = raw.get(field, [])
        if isinstance(values, list):
            cleaned = _clean_strings(values, limit=12)
            if cleaned:
                style[field] = cleaned
    for field in _STYLE_TEXT_FIELDS:
        value = raw.get(field, "")
        if isinstance(value, str) and value.strip():
            style[field] = value.strip()[:240]
    return style


def _normalize_claims(raw: object, source_type: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    channel = channel_for_source(source_type)
    claims: list[dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, Mapping):
            continue
        facet = str(item.get("facet", "")).strip()
        claim_key = str(item.get("claim_key", "")).strip()[:128]
        claim_value = item.get("claim_value", "")
        if facet not in _FACETS or not claim_key or not isinstance(claim_value, str):
            continue
        value = claim_value.strip()[:2000]
        if not value:
            continue

        context = item.get("context", {})
        normalized_context = dict(context) if isinstance(context, Mapping) else {}
        normalized_context["source_type"] = source_type
        if facet == "communication" and channel:
            normalized_context["channel"] = channel

        polarity = str(item.get("polarity", "uncertain")).strip()
        confidence = _confidence(item.get("confidence", 0.5))
        claims.append(
            {
                "facet": facet,
                "claim_key": claim_key,
                "claim_value": value,
                "context": normalized_context,
                "polarity": polarity if polarity in _POLARITIES else "uncertain",
                "confidence": confidence,
                "quote": str(item.get("quote", "")).strip()[:600],
            }
        )
    return claims


def _clean_strings(values: list[object], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()[:120]
        if cleaned and cleaned not in result:
            result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _confidence(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5
