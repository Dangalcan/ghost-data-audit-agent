"""JSON evidence: snapshot bundles, service resource exports, record-lookup ledgers,
export job lists, analytics reports."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..identity import tokens_from_values
from .base import (KIND_BY_RECORD_TYPE, Observation, ParsedEvidence, Scope, default_kind, flags_from_result,
                   is_null, presence_from_result, to_bool)

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
PII_KEYS = ("email", "phone", "email_hash", "full_name", "name", "display_name", "requester_email",
            "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email", "uploaded_by")
REF_KEYS = ("customer_reference", "subject_reference", "user_id", "subject_selector", "subject_reference")


def _flatten(rec: dict) -> dict:
    out = dict(rec)
    for k in ("document", "profile", "hits"):
        if isinstance(rec.get(k), dict):
            for kk, vv in rec[k].items():
                out.setdefault(kk, vv)
    return out


def _pii(rec: dict, kind: str) -> Optional[bool]:
    seen = False
    for k in PII_KEYS:
        if k in rec:
            seen = True
            if not is_null(rec[k]):
                return True
    if kind == "support_message" and isinstance(rec.get("message_text"), str):
        txt = rec["message_text"]
        return bool(tokens_from_values([txt]).emails) or ("@" in txt and "[REDACTED]" not in txt)
    if kind in ("profile", "export", "search_entry", "support_attachment", "object", "marketing_profile"):
        return True
    return False if seen else None


def _tokens(rec: dict):
    values = [rec.get(k) for k in PII_KEYS + REF_KEYS]
    if isinstance(rec.get("message_text"), str):
        values.append(rec["message_text"])
    if isinstance(rec.get("query"), str):
        values.append(rec["query"])
    if isinstance(rec.get("lookup_value"), str):
        values.append(rec["lookup_value"])
    return tokens_from_values(values)


def record_observation(rec: dict, kind: str, evidence_id: str, observed_at: str, source: str,
                       presence: str = "PRESENT") -> Optional[Observation]:
    rec = _flatten(rec)
    rid = rec.get("resource_id")
    if not isinstance(rid, str):
        return None
    pii = _pii(rec, kind)
    if kind == "tombstone":
        pii = any(not is_null(rec.get(k)) for k in ("name", "email", "phone", "email_hash"))
    enabled = to_bool(rec.get("enabled"))
    restore_allowed = to_bool(rec.get("restore_allowed"))
    retention = rec.get("retention_until") or rec.get("retained_until")
    extra = {k: rec.get(k) for k in ("account_status", "download_available", "last_delivery_status", "event_code",
                                      "event_name", "file_name", "archive_path", "object_key", "status_code")
             if k in rec}
    return Observation(resource_id=rid, evidence_id=evidence_id, observed_at=observed_at, presence=presence,
                       kind=kind, tokens=_tokens(rec), pii=pii, enabled=enabled, restore_allowed=restore_allowed,
                       retention_until=retention, source=source, extra=extra)


def ledger_observation(rec: dict, evidence_id: str, source: str, default_time: str) -> Optional[Observation]:
    """Rows shaped like {system, resource_id, record_type, result, subject_reference|lookup_value, recorded_at}."""
    rid = rec.get("resource_id")
    if not isinstance(rid, str):
        return None
    kind = KIND_BY_RECORD_TYPE.get(str(rec.get("record_type", "")).upper(), default_kind(rid))
    result = str(rec.get("result", ""))
    presence = presence_from_result(result)
    flags = flags_from_result(kind, result)
    subject = rec.get("subject_reference") or rec.get("lookup_value")
    return Observation(resource_id=rid, evidence_id=evidence_id, observed_at=rec.get("recorded_at") or default_time,
                       presence=presence, kind=kind, tokens=tokens_from_values([subject]), pii=flags.get("pii"),
                       enabled=flags.get("enabled"), restore_allowed=flags.get("restore_allowed"),
                       source=source, extra={"result": result, "record_type": rec.get("record_type")})


def parse_json(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    data = json.loads(open(path, encoding="utf-8").read())
    name = path.name
    if "snapshots" in data:
        for si, snap in enumerate(data["snapshots"]):
            coll, store, when = snap.get("collection", ""), snap.get("data_store", ""), snap.get("captured_at", captured_at)
            src = f"{name}:snapshots[{si}]"
            if coll == "operational_record_history":
                for ri, rec in enumerate(snap.get("records", [])):
                    obs = ledger_observation(rec, evidence_id, f"{src}[{ri}]", when)
                    if obs:
                        parsed.observations.append(obs)
                continue
            kind = COLLECTION_KIND.get(coll, "unknown")
            present = set()
            for ri, rec in enumerate(snap.get("records", [])):
                obs = record_observation(rec, kind, evidence_id, when, f"{src}[{ri}]")
                if obs:
                    parsed.observations.append(obs)
                    present.add(obs.resource_id)
            if not snap.get("records"):  # explicitly empty collection => complete (empty) view of that store
                parsed.scopes.append(Scope(system_id=store, evidence_id=evidence_id, observed_at=when, present=present))
    elif "service_exports" in data:
        when_default = data.get("exported_at", captured_at)
        for system, colls in data["service_exports"].items():
            for coll, recs in colls.items():
                for ri, rec in enumerate(recs):
                    src = f"{name}:{system}.{coll}[{ri}]"
                    when = rec.get("observed_at", when_default)
                    if coll == "profile_lookups":
                        presence = "ABSENT" if rec.get("status_code") == 404 or rec.get("profile") is None else "PRESENT"
                        obs = record_observation(rec, "profile", evidence_id, when, src, presence)
                    elif coll == "index_queries":
                        docs = (rec.get("hits") or {}).get("documents") or []
                        for di, doc in enumerate(docs):
                            doc = dict(doc)
                            doc.setdefault("query", rec.get("query"))
                            o = record_observation(doc, "search_entry", evidence_id, when, f"{src}.documents[{di}]")
                            if o:
                                parsed.observations.append(o)
                        continue
                    else:
                        kind = {"profiles": "analytics_profile", "restore_entries": "backup", "exports": "export",
                                "connectors": "connector", "audit_records": "audit_record", "tickets": "support_message"}.get(coll, "unknown")
                        obs = record_observation(rec, kind, evidence_id, when, src)
                    if obs:
                        parsed.observations.append(obs)
    elif "records" in data and data["records"] and "record_type" in data["records"][0]:
        when_default = data.get("exported_at", captured_at)
        for ri, rec in enumerate(data["records"]):
            obs = ledger_observation(rec, evidence_id, f"{name}:records[{ri}]", when_default)
            if obs:
                parsed.observations.append(obs)
    elif "records" in data:
        when = data.get("captured_at", captured_at)
        coll = data.get("collection", "")
        kind = COLLECTION_KIND.get(coll, "unknown")
        if kind == "unknown" and data["records"]:
            kind = default_kind(data["records"][0].get("resource_id", ""))
        for ri, rec in enumerate(data["records"]):
            obs = record_observation(rec, kind, evidence_id, when, f"{name}:records[{ri}]")
            if obs:
                parsed.observations.append(obs)
    else:
        parsed.notes.append("unrecognised json layout")
    return parsed
