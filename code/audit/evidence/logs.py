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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import RESOURCE_ID_RE
from ..identity import Tokens, tokens_from_values
from .base import (
    KIND_BY_RECORD_TYPE,
    Observation,
    ParsedEvidence,
    default_kind,
    flags_from_result,
    is_null,
    presence_from_result,
    to_bool,
)

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|\S+)')
TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")
BRACKET_RE = re.compile(r"\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w-]+)\]\s+(\w+):\s*(.*)$")
RID_RE = re.compile(RESOURCE_ID_RE)

NON_TERMINAL_EVENTS = {
    "connection_acquired",
    "cache_lookup",
    "dependency_retry",
    "dependency_recovered",
    "thumbnail_failed",
    "deletion_marker_check",
    "batch_started",
    "checkpoint_saved",
}
LOOKUP_KEYS = ("lookup_key", "subject_key", "match_key", "scan_key", "subject_ref", "lookup_value", "correlation_key")
PII_FIELDS = (
    "email",
    "phone",
    "email_hash",
    "requester_email",
    "requester_name",
    "requester_phone",
    "customer_email",
    "matched_value",
    "full_name",
    "display_name",
    "name",
)
REF_FIELDS = ("customer_reference", "subject_reference", "subject_ref", "correlation_key", "user_id")
ABSENT_LOOKUP_RESULTS = ("miss", "0")


@dataclass
class LogLine:
    ts: str
    level: str
    service: str
    trace: str | None
    event: str
    kv: dict[str, str]
    lineno: int
    raw: str


@dataclass
class Facts:
    """What one log line says about a record, before it becomes an Observation."""

    kind: str
    presence: str = "PRESENT"
    pii: bool | None = None
    enabled: bool | None = None
    restore_allowed: bool | None = None


# --------------------------------------------------------------------------- line parsing
def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in KV_RE.finditer(text):
        key, value, quoted = m.group(1), m.group(2), m.group(3)
        out[key] = quoted if quoted is not None else value
    return out


def _trace_or_none(token: str | None) -> str | None:
    return token if token and TRACE_RE.fullmatch(token) else None


def _parse_json_line(rest: str) -> tuple[str, str, str | None, dict[str, str]] | None:
    try:
        data = json.loads(rest)
    except json.JSONDecodeError:
        return None
    kv = {k: ("" if v is None else str(v)) for k, v in data.items()}
    return kv.get("severity", kv.get("level", "")), kv.get("service", kv.get("logger", "")), None, kv


def _parse_syslog_line(rest: str) -> tuple[str, str, str | None, dict[str, str]] | None:
    parts = rest.split(" ", 6)
    if len(parts) < 7:
        return None
    _, _host, service, trace_tok, _, _, tail = parts
    kv = _kv(tail)
    return kv.get("level", ""), service, _trace_or_none(trace_tok), kv


def _parse_pipe_line(rest: str) -> tuple[str, str, str | None, dict[str, str], str] | None:
    segs = [s.strip() for s in rest.split("|")[1:]]
    if len(segs) < 5:
        return None
    level, service, trace_tok, event, tail = segs[0], segs[1], segs[2], segs[3], " | ".join(segs[4:])
    return level, service, _trace_or_none(trace_tok), _kv(tail), event


def _parse_kv_line(rest: str) -> tuple[str, str, str | None, dict[str, str]]:
    kv = _kv(rest)
    if "message" in kv and "=" in kv["message"]:  # cloud runtime: escaped kv inside message
        kv.update(_kv(kv.pop("message").replace('\\"', '"')))
    service = kv.get("service", kv.get("function", kv.get("logger", "")))
    return kv.get("level", ""), service, _trace_or_none(kv.get("trace_id", kv.get("requestId"))), kv


def parse_line(raw: str, lineno: int) -> LogLine | None:
    """Normalise one raw line of any dialect; None when it is not a log line."""
    m = TS_RE.match(raw.strip())
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    event = ""
    if rest.startswith("{"):
        parsed = _parse_json_line(rest)
    elif rest.startswith("<"):
        parsed = _parse_syslog_line(rest)
    elif rest.startswith("|"):
        pipe = _parse_pipe_line(rest)
        parsed, event = (pipe[:4], pipe[4]) if pipe else (None, "")
    elif rest.startswith("["):
        m2 = BRACKET_RE.match(rest)
        parsed = (m2.group(1), m2.group(2), _trace_or_none(m2.group(3)), _kv(m2.group(5))) if m2 else None
        event = m2.group(4) if m2 else ""
    else:
        parsed = _parse_kv_line(rest)
    if parsed is None:
        return None
    level, service, trace, kv = parsed
    event = kv.get("event", event)
    service = kv.get("service", service) or service
    if trace is None:
        trace = _trace_or_none(kv.get("trace_id") or kv.get("requestId"))
    return LogLine(ts, level, service, trace, event, kv, lineno, raw)


# --------------------------------------------------------------------------- facts per event
def _q(kv: dict[str, str], key: str) -> str:
    return kv.get(key, "").strip('"')


def _pii_from_kv(kv: dict[str, str]) -> bool | None:
    seen = False
    for f in PII_FIELDS:
        if f in kv:
            seen = True
            if not is_null(kv[f]):
                return True
    if "direct_identifiers" in kv:
        try:
            return int(_q(kv, "direct_identifiers")) > 0
        except ValueError:
            pass
    return False if seen else None


def _tokens(kv: dict[str, str], started_kv: dict[str, str]) -> Tokens:
    values = [kv.get(f) for f in PII_FIELDS + REF_FIELDS + LOOKUP_KEYS]
    values += [started_kv.get(f) for f in LOOKUP_KEYS]
    values += [v for k, v in kv.items() if k in ("subject_selector", "lookup_key", "query")]
    return tokens_from_values(values)


def _rows_absent(kv: dict[str, str]) -> bool:
    return _q(kv, "rows") == "0"


def _facts_ledger(kv: dict[str, str], f: Facts) -> Facts:
    """record_lookup lines: explicit record_type + result vocabulary."""
    f.kind = KIND_BY_RECORD_TYPE.get(_q(kv, "record_type").upper(), f.kind)
    f.presence = presence_from_result(kv.get("result", ""))
    flags = flags_from_result(f.kind, kv.get("result", ""), _q(kv, "subject_reference"))
    f.pii = flags.get("pii", f.pii)
    f.enabled = flags.get("enabled", f.enabled)
    f.restore_allowed = flags.get("restore_allowed", f.restore_allowed)
    return f


def _facts_profile(kv: dict[str, str], f: Facts) -> Facts:
    f.presence = "ABSENT" if _q(kv, "http_status") == "404" or _rows_absent(kv) else "PRESENT"
    f.pii = True if f.presence == "PRESENT" else None
    return f


def _facts_search(kv: dict[str, str], f: Facts) -> Facts:
    res = _q(kv, "result").lower()
    f.kind = "search_entry"
    f.presence = "ABSENT" if res in ABSENT_LOOKUP_RESULTS or _q(kv, "total_hits") == "0" else "PRESENT"
    f.pii = True if f.presence == "PRESENT" else None
    return f


def _facts_contact(kind: str) -> Callable[[dict[str, str], Facts], Facts]:
    def handler(kv: dict[str, str], f: Facts) -> Facts:
        f.kind, f.presence, f.pii = kind, ("ABSENT" if _rows_absent(kv) else "PRESENT"), True
        return f

    return handler


def _facts_kind(kind: str, pii: bool | None = None, default_pii: bool | None = None) -> Callable:
    """Fixed kind; `pii` forces the flag, `default_pii` fills it only when the line says nothing."""

    def handler(kv: dict[str, str], f: Facts) -> Facts:
        f.kind = kind
        if pii is not None:
            f.pii = pii
        elif f.pii is None:
            f.pii = default_pii
        return f

    return handler


def _facts_audit(kv: dict[str, str], f: Facts) -> Facts:
    f.kind = "audit_record"
    f.presence = "ABSENT" if _rows_absent(kv) else "PRESENT"
    return f


def _facts_restore(kv: dict[str, str], f: Facts) -> Facts:
    f.kind = "backup"
    eligible, blocked = to_bool(kv.get("eligible")), to_bool(kv.get("deletion_block"))
    decision = _q(kv, "decision").lower()
    if eligible is not None:
        f.restore_allowed = eligible and not blocked
    elif decision in ("deny", "denied", "blocked"):
        f.restore_allowed = False
    elif decision in ("allow", "allowed", "approved"):
        f.restore_allowed = True
    return f


EVENT_HANDLERS: dict[str, Callable[[dict[str, str], Facts], Facts]] = {
    "profile_lookup_completed": _facts_profile,
    "index_query_completed": _facts_search,
    "contact_lookup_completed": _facts_contact("analytics_contact"),  # keyed by email hash only
    "contact_query_completed": _facts_contact("support_contact"),  # legacy contact index entry
    "attachment_pii_scan": _facts_kind("support_attachment", pii=True),
    "message_pii_scan": _facts_kind("support_message", pii=True),
    "support_ticket_read_completed": _facts_kind("support_message", default_pii=False),
    "message_read": _facts_kind("support_message", default_pii=False),
    "analytics_profile_read_completed": _facts_kind("analytics_profile", default_pii=False),
    "analytics_profile_read": _facts_kind("analytics_profile", default_pii=False),
    "analytics_identity_scan": _facts_kind("analytics_profile", default_pii=False),
    "audit_record_read_completed": _facts_audit,
    "connector_config_read_completed": _facts_kind("connector"),
    "connector_heartbeat": _facts_kind("connector"),
    "connector_config_loaded": _facts_kind("connector"),
    "restore_policy_read_completed": _facts_restore,
    "restore_candidate_evaluated": _facts_restore,
    "restore_authorization_completed": _facts_restore,
    "export_download_check_completed": _facts_kind("export", pii=True),
    "export_metadata_read_completed": _facts_kind("export", pii=True),
}


def _facts_generic(line: LogLine, f: Facts) -> Facts:
    """Lines without a dedicated handler: infer from the service and generic status fields."""
    kv = line.kv
    if line.service == "account_service" and "http_status" in kv:
        return _facts_profile(kv, f)
    if line.service == "search_service" and "result" in kv:
        return _facts_search(kv, f)
    if "http_status" in kv:
        f.presence = "ABSENT" if _q(kv, "http_status") == "404" else "PRESENT"
    elif _rows_absent(kv):
        f.presence = "ABSENT"
    return f


def line_to_observation(line: LogLine, started_kv: dict[str, str], evidence_id: str, source: str) -> Observation | None:
    """Turn one terminal log line about a resource into an Observation (None when it says nothing)."""
    kv = line.kv
    rid = _q(kv, "resource_id")
    if not RID_RE.fullmatch(rid) or line.event.endswith("_started") or line.event in NON_TERMINAL_EVENTS:
        return None
    facts = Facts(
        kind=default_kind(rid),
        pii=_pii_from_kv(kv),
        enabled=to_bool(kv.get("enabled")),
        restore_allowed=to_bool(kv.get("restore_allowed")),
    )
    if "record_type" in kv:
        facts = _facts_ledger(kv, facts)
    else:
        handler = EVENT_HANDLERS.get(line.event)
        facts = handler(kv, facts) if handler else _facts_generic(line, facts)
    if facts.kind == "profile" and facts.presence == "PRESENT":
        facts.pii = True
    retention = _q(kv, "retention_until") or None
    return Observation(
        resource_id=rid,
        evidence_id=evidence_id,
        observed_at=line.ts,
        presence=facts.presence,
        kind=facts.kind,
        tokens=_tokens(kv, started_kv),
        pii=facts.pii,
        enabled=facts.enabled,
        restore_allowed=facts.restore_allowed,
        retention_until=retention,
        source=source,
        extra={"event": line.event, "service": line.service, "trace": line.trace},
    )


# --------------------------------------------------------------------------- file parsing
def parse_log(path: Path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    lines: list[LogLine] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh, 1):
            ll = parse_line(raw, i)
            if ll:
                lines.append(ll)
    by_trace: dict[str, dict[str, str]] = {}
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
