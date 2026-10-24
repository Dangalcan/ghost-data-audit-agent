"""Plain-text handover notes and PDF documents."""

from __future__ import annotations

import re

from ..config import RESOURCE_ID_RE
from ..identity import extract_tokens, tokens_from_values
from .base import (
    KIND_BY_RECORD_TYPE,
    Observation,
    ParsedEvidence,
    default_kind,
    flags_from_result,
    presence_from_result,
)

HANDOVER_RE = re.compile(r"^-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.*)$")
RID_RE = re.compile(RESOURCE_ID_RE)


def parse_text(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    for i, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = HANDOVER_RE.match(raw.strip())
        if not m:
            continue
        when, system, rid, rtype, result, subject = m.groups()
        kind = KIND_BY_RECORD_TYPE.get(rtype.upper(), default_kind(rid))
        flags = flags_from_result(kind, result, subject.strip())
        parsed.observations.append(
            Observation(
                resource_id=rid,
                evidence_id=evidence_id,
                observed_at=when,
                presence=presence_from_result(result),
                kind=kind,
                tokens=tokens_from_values([subject.strip()]),
                pii=flags.get("pii"),
                enabled=flags.get("enabled"),
                restore_allowed=flags.get("restore_allowed"),
                source=f"{path.name}:{i}",
                extra={"result": result, "record_type": rtype},
            )
        )
    return parsed


def pdf_text(path) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:  # pragma: no cover - fallback extractor
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)


MONTHS = {
    m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)
}


def _dates(text: str) -> list[str]:
    out = []
    for d, mon, y, hh, mm in re.findall(r"(\d{1,2}) (\w{3}) (\d{4})(?: (\d{2}) (\d{2}))?", text):
        if mon in MONTHS:
            out.append(f"{y}-{MONTHS[mon]:02d}-{int(d):02d}T{hh or '00'}:{mm or '00'}:00Z")
    return out


def parse_pdf(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    """Business documents (invoices, support attachments, journal extracts, registers).

    They carry no verdicts; we record every resource id they mention together with
    every identifier they contain. Kind follows the document type where obvious.
    """
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    text = pdf_text(path)
    tokens = extract_tokens(text)
    rids = sorted(set(RID_RE.findall(text)))
    low = text.lower()
    doc_kind: str | None = None
    if "attachment" in low or "support reference" in low:
        doc_kind = "support_attachment"
    elif "statement" in low or "invoice" in low:
        doc_kind = "export"
    retention = None
    m = re.search(r"retain until (\d{1,2} \w{3} \d{4}(?: \d{2} \d{2})?)", text)
    if m:
        ds = _dates(m.group(1))
        retention = ds[0] if ds else None
    for rid in rids:
        kind = default_kind(rid)
        if doc_kind and rid.startswith("support_"):
            kind = doc_kind
        parsed.observations.append(
            Observation(
                resource_id=rid,
                evidence_id=evidence_id,
                observed_at=captured_at,
                presence="PRESENT",
                kind=kind,
                tokens=tokens,
                pii=bool(tokens.emails or tokens.phones),
                retention_until=retention,
                source=f"{path.name}",
                extra={"doc_kind": doc_kind},
            )
        )
    parsed.notes.append(f"pdf tokens: {sorted(tokens.all)}; resource ids: {rids}")
    parsed.extra_text = text  # type: ignore[attr-defined]
    return parsed
