"""SQLite-compatible snapshot exports.

Topologies: inventory (typed tables + snapshot_scope), read/reporting/failover replicas
(customer_data_<replica> tables holding record_json), regional replicas (ap_south / eu_west),
accounts failover (customer_profiles_primary / _failover_replica), search primary.
Every copy seen in any table becomes an observation (replica name kept), so drift
between primary and replica is visible to the state engine.

Security: a snapshot is untrusted input. It runs in an in-memory connection under an
authorizer that denies ATTACH / DETACH / extension loading and every PRAGMA except
foreign_keys, and a script containing a file-access statement (ATTACH, VACUUM INTO) is
rejected before execution. A rejected file yields no observations and is logged.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .. import log
from .base import Observation, ParsedEvidence, Scope
from .records_json import COLLECTION_KIND, record_observation

logger = log.get("evidence.sql")

TABLE_KIND = {
    "customer_profiles": "profile",
    "customer_search_documents": "search_entry",
    "customer_analytics_profiles": "analytics_profile",
    "support_records": "support_message",
    "customer_exports": "export",
    "connector_subscriptions": "connector",
    "backup_restore_entries": "backup",
    "security_audit_records": "audit_record",
    "search_documents_primary": "search_entry",
}
TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
REPLICA_SUFFIXES = (
    "_failover_replica",
    "_primary",
    "_read_replica",
    "_reporting_replica",
    "_replica_ap_south",
    "_replica_eu_west",
)
ALLOWED_PRAGMAS = {"foreign_keys"}
UNSAFE_STATEMENT_RE = re.compile(r"(?:^|;)\s*(?:ATTACH|DETACH|VACUUM|PRAGMA(?!\s+foreign_keys\b))\b", re.I | re.M)


# --------------------------------------------------------------------------- sandboxed execution
def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None) -> int:
    """Deny everything that could touch the file system or load code."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def load_script(script: str) -> sqlite3.Connection:
    """Execute an evidence snapshot in a sandboxed in-memory database."""
    if UNSAFE_STATEMENT_RE.search(script):
        raise ValueError("file-access statement (ATTACH / DETACH / VACUUM / PRAGMA) in evidence SQL")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.set_authorizer(_authorizer)
    con.executescript(script)
    return con


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# --------------------------------------------------------------------------- rows -> observations
def _split_replica(table: str) -> tuple[str, str | None]:
    for suffix in REPLICA_SUFFIXES:
        if table.endswith(suffix):
            return table[: -len(suffix)], suffix.lstrip("_")
    return table, None


def _row_observation(row: dict, table: str, base: str, evidence_id: str, when: str, src: str) -> Observation | None:
    if "record_json" in row:  # generic customer_data_* tables
        rec = json.loads(row["record_json"])
        kind = COLLECTION_KIND.get(row.get("collection_name", ""), "unknown")
        return record_observation(rec, kind, evidence_id, row.get("updated_at") or when, src)
    kind = TABLE_KIND.get(base, TABLE_KIND.get(table, "unknown"))
    if kind == "unknown":
        return None
    rec = dict(row)
    if "customer_reference" not in rec and "user_id" in rec:
        rec["customer_reference"] = rec["user_id"]
    return record_observation(rec, kind, evidence_id, when, src)


def _scope_system(row: dict, table: str, base: str, obs: Observation) -> str:
    if "record_json" in row:
        return row.get("origin_system") or obs.system_id
    return TABLE_SYSTEM.get(base, obs.system_id)


def parse_sql(path: Path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    script = path.read_text(encoding="utf-8")
    m = re.search(r"Captured at:\s*(\S+)", script)
    when = m.group(1) if m else captured_at
    try:
        con = load_script(script)
    except (ValueError, sqlite3.Error) as exc:
        logger.warning(log.kv(evidence=evidence_id, rejected_sql=type(exc).__name__))
        parsed.notes.append(f"rejected: {exc}")
        return parsed
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope_systems: dict[str, set[str]] = {}
    if "snapshot_scope" in tables:
        for r in con.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
            scope_systems.setdefault(r["origin_system"], set())
            when = r["captured_at"] or when
    for table in tables:
        if table == "snapshot_scope":
            continue
        base, replica = _split_replica(table)
        for i, r in enumerate(con.execute(f"SELECT * FROM {_quote(table)}")):
            row = dict(r)
            obs = _row_observation(row, table, base, evidence_id, when, f"{path.name}:{table}[{i}]")
            if obs is None:
                continue
            obs.replica = replica or table
            parsed.observations.append(obs)
            scope_systems.setdefault(_scope_system(row, table, base, obs), set()).add(obs.resource_id)
    con.close()
    for system, present in scope_systems.items():
        parsed.scopes.append(Scope(system_id=system, evidence_id=evidence_id, observed_at=when, present=present))
    return parsed
