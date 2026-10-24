"""Log parsers: eight dialects normalised into (timestamp, service, trace, event, kv).

Dialects seen in dataset/evidence/logs:
  kv            2026-09-15T08:51:43Z level=DEBUG service=x trace_id=tr-1 event=e k=v k="v"
  json          2026-09-10T08:56:15Z {"severity":"DEBUG","service":"x","trace_id":"tr-1","event":"e",...}
  syslog        2026-08-22T10:51:11Z <15>1 node-1 service tr-1 - - level=DEBUG k="v"
  pipe          2026-07-03T07:51:30Z | DEBUG | service | tr-1 | event | k="v"
  bracket       2026-07-28T05:51:16Z [DEBUG] [service] [tr-1] event: k="v"
  cloud         2026-09-30T11:51:01Z requestId=tr-1 level=DEBUG function=service message="k=\"v\" ..."
  record_lookup 2026-09-06T07:00:00Z INFO service=x event=record_lookup seq=1 system="..." resource_id="..." result="..."
  readback      2026-03-10T06:18:01Z level=INFO service=x trace_id=rb-1 event=profile_lookup_started lookup_key=...

Lines of one trace are joined so the *_started line's lookup key (email / user id)
becomes an identifier of the resource reported by the *_completed line.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import RESOURCE_ID_RE
from ..identity import tokens_from_values
from .base import KIND_BY_RECORD_TYPE, Observation, ParsedEvidence, default_kind, flags_from_result, is_null, presence_from_result, to_bool

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|\S+)')
TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")
RID_RE = re.compile(RESOURCE_ID_RE)

NON_TERMINAL_EVENTS = {"connection_acquired", "cache_lookup", "dependency_retry", "dependency_recovered",
                       "thumbnail_failed", "deletion_marker_check", "batch_started", "checkpoint_saved"}
LOOKUP_KEYS = ("lookup_key", "subject_key", "match_key", "scan_key", "subject_ref", "lookup_value", "correlation_key")
PII_FIELDS = ("email", "phone", "email_hash", "requester_email", "requester_name", "requester_phone",
              "customer_email", "matched_value", "full_name", "display_name", "name")
REF_FIELDS = ("customer_reference", "subject_reference", "subject_ref", "correlation_key", "user_id")


@dataclass
class LogLine:
    ts: str
    level: str
    service: str
    trace: Optional[str]
    event: str
    kv: Dict[str, str]
    lineno: int
    raw: str


def _kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in KV_RE.finditer(text):
        key, value, quoted = m.group(1), m.group(2), m.group(3)
        out[key] = quoted if quoted is not None else value
    return out


def parse_line(raw: str, lineno: int) -> Optional[LogLine]:
    m = TS_RE.match(raw.strip())
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    level, service, trace, event, kv = "", "", None, "", {}
    if rest.startswith("{"):
        try:
            data = json.loads(rest)
        except json.JSONDecodeError:
            return None
        kv = {k: ("" if v is None else str(v)) for k, v in data.items()}
        level = kv.get("severity", kv.get("level", ""))
        service = kv.get("service", kv.get("logger", ""))
    elif rest.startswith("<"):  # syslog RFC5424
        parts = rest.split(" ", 6)
        if len(parts) >= 7:
            _, host, service, trace_tok, _, _, tail = parts
            trace = trace_tok if TRACE_RE.fullmatch(trace_tok) else None
            kv = _kv(tail)
            level = kv.get("level", "")
    elif " | " in rest and rest.startswith("|"):
        segs = [s.strip() for s in rest.split("|")[1:]]
        if len(segs) >= 5:
            level, service, trace_tok, event, tail = segs[0], segs[1], segs[2], segs[3], " | ".join(segs[4:])
            trace = trace_tok if TRACE_RE.fullmatch(trace_tok) else None
            kv = _kv(tail)
    elif rest.startswith("["):
        m2 = re.match(r"\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w-]+)\]\s+(\w+):\s*(.*)$", rest)
        if m2:
            level, service, trace_tok, event, tail = m2.groups()
            trace = trace_tok if TRACE_RE.fullmatch(trace_tok) else None
            kv = _kv(tail)
    else:
        kv = _kv(rest)
        if "message" in kv and "=" in kv["message"]:  # cloud runtime: escaped kv inside message
            inner = _kv(kv.pop("message").replace('\\"', '"'))
            kv.update(inner)
        level = kv.get("level", "")
        service = kv.get("service", kv.get("function", kv.get("logger", "")))
        trace = kv.get("trace_id", kv.get("requestId"))
        if trace and not TRACE_RE.fullmatch(trace):
            trace = None
    event = kv.get("event", event)
    service = kv.get("service", service) or service
    if trace is None:
        trace = kv.get("trace_id") or kv.get("requestId")
        if trace and not TRACE_RE.fullmatch(trace):
            trace = None
    return LogLine(ts, level, service, trace, event, kv, lineno, raw)


def _pii_from_kv(kv: Dict[str, str]) -> Optional[bool]:
    seen = False
    for f in PII_FIELDS:
        if f in kv:
            seen = True
            if not is_null(kv[f]):
                return True
    if "direct_identifiers" in kv:
        try:
            return int(kv["direct_identifiers"].strip('"')) > 0
        except ValueError:
            pass
    return False if seen else None


def _tokens(kv: Dict[str, str], started_kv: Dict[str, str]) -> "tokens_from_values":
    values = [kv.get(f) for f in PII_FIELDS + REF_FIELDS + LOOKUP_KEYS]
    values += [started_kv.get(f) for f in LOOKUP_KEYS]
    values += [v for k, v in kv.items() if k in ("subject_selector", "lookup_key", "query")]
    return tokens_from_values(values)


def line_to_observation(line: LogLine, started_kv: Dict[str, str], evidence_id: str, source: str) -> Optional[Observation]:
    kv = line.kv
    rid = kv.get("resource_id", "").strip('"')
    if not RID_RE.fullmatch(rid):
        return None
    ev = line.event
    if ev.endswith("_started") or ev in NON_TERMINAL_EVENTS:
        return None
    kind = default_kind(rid)
    presence = "PRESENT"
    pii = _pii_from_kv(kv)
    enabled = to_bool(kv.get("enabled"))
    restore_allowed = to_bool(kv.get("restore_allowed"))
    retention = kv.get("retention_until")
    tokens = _tokens(kv, started_kv)

    if "record_type" in kv:  # record_lookup ledger lines
        kind = KIND_BY_RECORD_TYPE.get(kv["record_type"].strip('"').upper(), kind)
        presence = presence_from_result(kv.get("result", ""))
        flags = flags_from_result(kind, kv.get("result", ""))
        pii = flags.get("pii", pii)
        enabled = flags.get("enabled", enabled)
        restore_allowed = flags.get("restore_allowed", restore_allowed)
    elif ev in ("profile_lookup_completed",) or (line.service == "account_service" and "http_status" in kv):
        status = kv.get("http_status", "").strip('"')
        rows = kv.get("rows", "").strip('"')
        presence = "ABSENT" if status == "404" or rows == "0" else "PRESENT"
        pii = True if presence == "PRESENT" else None
    elif ev in ("index_query_completed",) or (line.service == "search_service" and "result" in kv):
        res = kv.get("result", "").strip('"').lower()
        hits = kv.get("total_hits", "").strip('"')
        presence = "ABSENT" if res in ("miss", "0") or hits == "0" else "PRESENT"
        kind = "search_entry"
        pii = True if presence == "PRESENT" else None
    elif ev == "contact_lookup_completed":  # analytics contact entry keyed by email hash only
        kind = "analytics_contact"
        presence = "ABSENT" if kv.get("rows", "").strip('"') == "0" else "PRESENT"
        pii = True
    elif ev == "contact_query_completed":   # support legacy contact index entry
        kind = "support_contact"
        presence = "ABSENT" if kv.get("rows", "").strip('"') == "0" else "PRESENT"
        pii = True
    elif ev in ("attachment_pii_scan",):
        kind = "support_attachment"
        pii = True
    elif ev in ("message_pii_scan",):
        kind = "support_message"
        pii = True
    elif ev in ("support_ticket_read_completed", "message_read"):
        kind = "support_message"
        if pii is None:
            pii = False
    elif ev in ("analytics_profile_read_completed", "analytics_profile_read", "analytics_identity_scan"):
        kind = "analytics_profile"
        if pii is None:
            pii = False
    elif ev in ("audit_record_read_completed",):
        kind = "audit_record"
        presence = "ABSENT" if kv.get("rows", "").strip('"') == "0" else "PRESENT"
    elif ev in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
        kind = "connector"
    elif ev in ("restore_policy_read_completed", "restore_candidate_evaluated", "restore_authorization_completed"):
        kind = "backup"
        if ev == "restore_candidate_evaluated":
            eligible, blocked = to_bool(kv.get("eligible")), to_bool(kv.get("deletion_block"))
            if eligible is not None:
                restore_allowed = eligible and not blocked
        elif ev == "restore_authorization_completed":
            decision = kv.get("decision", "").strip('"').lower()
            if decision in ("deny", "denied", "blocked"):
                restore_allowed = False
            elif decision in ("allow", "allowed", "approved"):
                restore_allowed = True
    elif ev in ("export_download_check_completed", "export_metadata_read_completed"):
        kind = "export"
        pii = True
    elif ev == "deletion_marker_check":
        return None
    elif "http_status" in kv:
        presence = "ABSENT" if kv["http_status"].strip('"') == "404" else "PRESENT"
    elif "rows" in kv and kv["rows"].strip('"') == "0":
        presence = "ABSENT"
    if kind == "profile" and presence == "PRESENT":
        pii = True
    return Observation(resource_id=rid, evidence_id=evidence_id, observed_at=line.ts, presence=presence,
                       kind=kind, tokens=tokens, pii=pii, enabled=enabled, restore_allowed=restore_allowed,
                       retention_until=(retention.strip('"') if retention else None), source=source,
                       extra={"event": ev, "service": line.service, "trace": line.trace})


def parse_log(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    lines: List[LogLine] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh, 1):
            ll = parse_line(raw, i)
            if ll:
                lines.append(ll)
    by_trace: Dict[str, Dict[str, str]] = {}
    for ll in lines:
        if ll.trace:
            keys = {k: v for k, v in ll.kv.items() if k in LOOKUP_KEYS}
            if keys:
                by_trace.setdefault(ll.trace, {}).update(keys)
    for ll in lines:
        started_kv = by_trace.get(ll.trace, {}) if ll.trace else {}
        obs = line_to_observation(ll, started_kv, evidence_id, f"{path.name}:{ll.lineno}")
        if obs:
            parsed.observations.append(obs)
    return parsed
