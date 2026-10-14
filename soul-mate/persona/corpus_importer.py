"""素材导入入口：只产生可追溯的候选，不直接改写运行时人格。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from persona.dossier import add_persona_claim, add_source_material, link_claim_evidence
from persona.style_analyzer import (
    analyze_style,
    channel_for_source,
    extract_persona_claims,
    normalize_source_type,
)


async def import_corpus(
    text: str,
    source_type: str,
    *,
    source_time: str | None = None,
    reliability: float = 0.5,
    import_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """导入原始素材并生成待审核结论，运行时快照保持不变。"""
    content = text.strip()
    if not content:
        return {"success": False, "error": "内容为空"}

    try:
        normalized_source = normalize_source_type(source_type)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    material = add_source_material(
        content=content,
        source_type=normalized_source,
        source_time=source_time,
        reliability=reliability,
        import_meta={"importer": "corpus_importer", **(import_meta or {})},
    )
    style, extracted_claims = await asyncio.gather(
        analyze_style(content, normalized_source),
        extract_persona_claims(content, normalized_source),
    )

    candidates: list[dict[str, Any]] = []
    channel = channel_for_source(normalized_source)
    if style and channel:
        style_claim = add_persona_claim(
            facet="communication",
            claim_key="style_profile",
            claim_value=json.dumps(style, ensure_ascii=False, sort_keys=True),
            context={"channel": channel, "source_type": normalized_source},
            polarity="supports",
            confidence=0.5,
            origin="style_analysis",
        )
        link_claim_evidence(
            claim_id=style_claim["id"],
            source_material_id=material["id"],
            quote_span={"kind": "whole_material"},
            support_kind="supports",
            evidence_weight=0.5,
        )
        candidates.append({**style_claim, "kind": "style"})

    for extracted in extracted_claims:
        claim = add_persona_claim(
            facet=extracted["facet"],
            claim_key=extracted["claim_key"],
            claim_value=extracted["claim_value"],
            context=extracted["context"],
            polarity=extracted["polarity"],
            confidence=extracted["confidence"],
            origin="material_analysis",
        )
        quote_span = _quote_span(content, extracted.get("quote", ""))
        link_claim_evidence(
            claim_id=claim["id"],
            source_material_id=material["id"],
            quote_span=quote_span,
            support_kind=_evidence_kind(extracted["polarity"]),
            evidence_weight=extracted["confidence"],
        )
        candidates.append({**claim, "kind": "persona"})

    return {
        "success": True,
        "material": material,
        "source_type": normalized_source,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _evidence_kind(polarity: str) -> str:
    return "contradicts" if polarity == "contradicts" else "supports"


def _quote_span(content: str, quote: str) -> dict[str, Any]:
    cleaned = quote.strip()
    if not cleaned:
        return {"kind": "whole_material"}
    start = content.find(cleaned)
    if start < 0:
        return {"kind": "model_quote", "quote": cleaned}
    return {"start": start, "end": start + len(cleaned), "quote": cleaned}
