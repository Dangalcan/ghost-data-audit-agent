"""Screenshots of internal consoles.

The model only *describes* the screenshot (structured extraction with a fixed JSON
schema, see prompts/vision_extract.md); every decision is made by code. Extractions
are cached in code/cache/vision/<file_id>.json so a full run is reproducible and
costs nothing when the cache is warm.
"""
from __future__ import annotations

import json
from typing import Optional

from ..config import VISION_CACHE_DIR
from ..identity import tokens_from_values
from .base import KIND_BY_RECORD_TYPE, Observation, ParsedEvidence, default_kind, is_null, to_bool


def cached_extraction(evidence_id: str) -> Optional[dict]:
    p = VISION_CACHE_DIR / f"{evidence_id}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def extraction_to_observations(extraction: dict, evidence_id: str, captured_at: str, source: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    when = extraction.get("captured_at") or captured_at
    for i, rec in enumerate(extraction.get("records", [])):
        rid = rec.get("resource_id")
        if not isinstance(rid, str) or not rid:
            continue
        rtype = str(rec.get("record_type") or "").upper()
        kind = KIND_BY_RECORD_TYPE.get(rtype, default_kind(rid))
        ids = rec.get("identifiers") or {}
        values = []
        for k in ("emails", "user_ids", "phones", "hashes", "names"):
            values += list(ids.get(k) or [])
        tokens = tokens_from_values(values)
        pii = bool(ids.get("emails") or ids.get("phones") or ids.get("hashes") or ids.get("names"))
        status = str(rec.get("status") or "").upper()
        presence = "ABSENT" if status in ("ABSENT", "NOT_FOUND", "DELETED") else "PRESENT"
        parsed.observations.append(Observation(
            resource_id=rid, evidence_id=evidence_id, observed_at=when, presence=presence, kind=kind,
            tokens=tokens, pii=pii if kind not in ("profile", "search_entry", "export", "support_attachment",
                                                    "object", "marketing_profile") else True,
            enabled=to_bool(rec.get("enabled")), restore_allowed=to_bool(rec.get("restore_allowed")),
            retention_until=None if is_null(rec.get("retention_until")) else rec.get("retention_until"),
            source=f"{source}:records[{i}]", extra={"aliases": rec.get("aliases") or [], "notes": rec.get("notes")}))
    parsed.notes.append(extraction.get("summary", ""))
    parsed.aliases = {a: r.get("resource_id") for r in extraction.get("records", []) for a in (r.get("aliases") or [])}  # type: ignore[attr-defined]
    return parsed


def parse_image(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    extraction = cached_extraction(evidence_id)
    if extraction is None:
        try:
            from ..llm import extract_screenshot  # lazy: only when the cache is cold
            extraction = extract_screenshot(path, evidence_id)
        except Exception as exc:  # no key / offline: the screenshot contributes nothing
            return ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at,
                                  notes=[f"screenshot not extracted ({type(exc).__name__}: {exc})"])
    return extraction_to_observations(extraction, evidence_id, captured_at, path.name)
