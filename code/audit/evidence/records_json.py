"""JSON evidence: snapshot bundles, service resource exports, record-lookup ledgers,
export job lists, analytics reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..identity import Tokens, tokens_from_values
from .base import (
    KIND_BY_RECORD_TYPE,
    Observation,
    ParsedEvidence,
    Scope,
    default_kind,
    flags_from_result,
    is_null,
    presence_from_result,
    to_bool,
)

COLLECTION_KIND = {
    "customer_profiles": "profile",
    "customer_search_documents": "search_entry",
    "customer_analytics_reports": "analytics_profile",
    "backup_catalog": "backup",
    "customer_export_jobs": "export",
    "connector_configurations": "connector",
    "security_audit_events": "audit_record",
    "ticket_attachments": "support_attachment",
    "support_conversations": "support_message",
    "deleted_customer_references": "tombstone",
    "retained_order_records": "legal_hold",
}
SERVICE_EXPORT_KIND = {
    "profiles": "analytics_profile",
    "restore_entries": "backup",
    "exports": "export",
    "connectors": "connector",
    "audit_records": "audit_record",
    "tickets": "support_message",
}
PII_KEYS = (
    "email",
    "phone",
    "email_hash",
    "full_name",
    "name",
    "display_name",
    "requester_email",
    "requester_name",
    "requester_phone",
    "owner_email",
    "owner_phone",
    "customer_email",
    "uploaded_by",
)
REF_KEYS = ("customer_reference", "subject_reference", "user_id", "subject_selector")
EXTRA_KEYS = (
    "account_status",
    "download_available",
    "last_delivery_status",
    "event_code",
    "event_name",
    "file_name",
    "archive_path",
    "object_key",
    "status_code",
    "expires_at",
)
ALWAYS_PII_KINDS = ("profile", "export", "search_entry", "support_attachment", "object", "marketing_profile")


# --------------------------------------------------------------------------- one record
def _flatten(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    for k in ("document", "profile", "hits"):
        if isinstance(rec.get(k), dict):
            for kk, vv in rec[k].items():
                out.setdefault(kk, vv)
    return out


def _pii(rec: dict[str, Any], kind: str) -> bool | None:
    seen = False
    for k in PII_KEYS:
        if k in rec:
            seen = True
            if not is_null(rec[k]):
                return True
    if kind == "support_message" and isinstance(rec.get("message_text"), str):
        txt = rec["message_text"]
        return bool(tokens_from_values([txt]).emails) or ("@" in txt and "[REDACTED]" not in txt)
    if kind in ALWAYS_PII_KINDS:
        return True
    return False if seen else None


def _tokens(rec: dict[str, Any]) -> Tokens:
    values = [rec.get(k) for k in PII_KEYS + REF_KEYS]
    values += [rec[k] for k in ("message_text", "query", "lookup_value") if isinstance(rec.get(k), str)]
    return tokens_from_values(values)


def _export_presence(rec: dict[str, Any], observed_at: str, presence: str) -> str:
    """A download package that is unavailable or past its expiry no longer exists."""
    if rec.get("download_available") is False:
        return "ABSENT"
    if isinstance(rec.get("expires_at"), str) and rec["expires_at"] <= observed_at:
        return "ABSENT"
    return presence


def record_observation(
    rec: dict[str, Any], kind: str, evidence_id: str, observed_at: str, source: str, presence: str = "PRESENT"
) -> Observation | None:
    rec = _flatten(rec)
    rid = rec.get("resource_id")
    if not isinstance(rid, str):
        return None
    pii = _pii(rec, kind)
    if kind == "tombstone":
        pii = any(not is_null(rec.get(k)) for k in ("name", "email", "phone", "email_hash"))
    if kind == "export":
        presence = _export_presence(rec, observed_at, presence)
    return Observation(
        resource_id=rid,
        evidence_id=evidence_id,
        observed_at=observed_at,
        presence=presence,
        kind=kind,
        tokens=_tokens(rec),
        pii=pii,
        enabled=to_bool(rec.get("enabled")),
        restore_allowed=to_bool(rec.get("restore_allowed")),
        retention_until=rec.get("retention_until") or rec.get("retained_until"),
        source=source,
        extra={k: rec.get(k) for k in EXTRA_KEYS if k in rec},
    )


def ledger_observation(rec: dict[str, Any], evidence_id: str, source: str, default_time: str) -> Observation | None:
    """Rows shaped like {system, resource_id, record_type, result, subject_reference|lookup_value, recorded_at}."""
    rid = rec.get("resource_id")
    if not isinstance(rid, str):
        return None
    kind = KIND_BY_RECORD_TYPE.get(str(rec.get("record_type", "")).upper(), default_kind(rid))
    result = str(rec.get("result", ""))
    subject = rec.get("subject_reference") or rec.get("lookup_value")
    flags = flags_from_result(kind, result, subject)
    return Observation(
        resource_id=rid,
        evidence_id=evidence_id,
        observed_at=rec.get("recorded_at") or default_time,
        presence=presence_from_result(result),
        kind=kind,
        tokens=tokens_from_values([subject]),
        pii=flags.get("pii"),
        enabled=flags.get("enabled"),
        restore_allowed=flags.get("restore_allowed"),
        source=source,
        extra={"result": result, "record_type": rec.get("record_type")},
    )


# --------------------------------------------------------------------------- layouts
def _parse_bundle(data: dict[str, Any], parsed: ParsedEvidence, name: str, captured_at: str) -> None:
    """records_assurance snapshot bundle: several collections, each with its own capture time."""
    for si, snap in enumerate(data["snapshots"]):
        coll, store = snap.get("collection", ""), snap.get("data_store", "")
        when = snap.get("captured_at", captured_at)
        src = f"{name}:snapshots[{si}]"
        records = snap.get("records", [])
        if coll == "operational_record_history":
            parsed.observations.extend(
                o
                for ri, rec in enumerate(records)
                if (o := ledger_observation(rec, parsed.evidence_id, f"{src}[{ri}]", when))
            )
            continue
        kind = COLLECTION_KIND.get(coll, "unknown")
        present: set[str] = set()
        for ri, rec in enumerate(records):
            obs = record_observation(rec, kind, parsed.evidence_id, when, f"{src}[{ri}]")
            if obs:
                parsed.observations.append(obs)
                present.add(obs.resource_id)
        if not records:  # explicitly empty collection => complete (empty) view of that store
            parsed.scopes.append(
                Scope(system_id=store, evidence_id=parsed.evidence_id, observed_at=when, present=present)
            )


def _parse_service_export(data: dict[str, Any], parsed: ParsedEvidence, name: str, captured_at: str) -> None:
    """service_resource_export: per-service readbacks with an observed_at per record."""
    when_default = data.get("exported_at", captured_at)
    for system, colls in data["service_exports"].items():
        for coll, recs in colls.items():
            for ri, rec in enumerate(recs):
                src = f"{name}:{system}.{coll}[{ri}]"
                when = rec.get("observed_at", when_default)
                if coll == "profile_lookups":
                    presence = "ABSENT" if rec.get("status_code") == 404 or rec.get("profile") is None else "PRESENT"
                    obs = record_observation(rec, "profile", parsed.evidence_id, when, src, presence)
                elif coll == "index_queries":
                    _parse_index_query(rec, parsed, src, when)
                    continue
                else:
                    kind = SERVICE_EXPORT_KIND.get(coll, "unknown")
                    obs = record_observation(rec, kind, parsed.evidence_id, when, src)
                if obs:
                    parsed.observations.append(obs)


def _parse_index_query(rec: dict[str, Any], parsed: ParsedEvidence, src: str, when: str) -> None:
    docs = (rec.get("hits") or {}).get("documents") or []
    for di, doc in enumerate(docs):
        enriched = {**doc, "query": doc.get("query", rec.get("query"))}
        obs = record_observation(enriched, "search_entry", parsed.evidence_id, when, f"{src}.documents[{di}]")
        if obs:
            parsed.observations.append(obs)


def _parse_ledger(data: dict[str, Any], parsed: ParsedEvidence, name: str, captured_at: str) -> None:
    when_default = data.get("exported_at", captured_at)
    for ri, rec in enumerate(data["records"]):
        obs = ledger_observation(rec, parsed.evidence_id, f"{name}:records[{ri}]", when_default)
        if obs:
            parsed.observations.append(obs)


def _parse_collection(data: dict[str, Any], parsed: ParsedEvidence, name: str, captured_at: str) -> None:
    when = data.get("captured_at", captured_at)
    kind = COLLECTION_KIND.get(data.get("collection", ""), "unknown")
    if kind == "unknown" and data["records"]:
        kind = default_kind(data["records"][0].get("resource_id", ""))
    for ri, rec in enumerate(data["records"]):
        obs = record_observation(rec, kind, parsed.evidence_id, when, f"{name}:records[{ri}]")
        if obs:
            parsed.observations.append(obs)


def parse_json(path: Path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    data = json.loads(path.read_text(encoding="utf-8"))
    name = path.name
    if "snapshots" in data:
        _parse_bundle(data, parsed, name, captured_at)
    elif "service_exports" in data:
        _parse_service_export(data, parsed, name, captured_at)
    elif data.get("records") and "record_type" in data["records"][0]:
        _parse_ledger(data, parsed, name, captured_at)
    elif "records" in data:
        _parse_collection(data, parsed, name, captured_at)
    else:
        parsed.notes.append("unrecognised json layout")
    return parsed
