"""SQLite-compatible snapshot exports.

Topologies: inventory (typed tables + snapshot_scope), read/reporting/failover replicas
(customer_data_<replica> tables holding record_json), regional replicas (ap_south / eu_west),
accounts failover (customer_profiles_primary / _failover_replica), search primary.
Every copy seen in any table becomes an observation (replica name kept), so drift
between primary and replica is visible to the state engine.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, List, Set

from .base import Observation, ParsedEvidence, Scope
from .records_json import COLLECTION_KIND, record_observation

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


def parse_sql(path, evidence_id: str, captured_at: str) -> ParsedEvidence:
    parsed = ParsedEvidence(evidence_id=evidence_id, captured_at=captured_at)
    script = open(path, encoding="utf-8").read()
    m = re.search(r"Captured at:\s*(\S+)", script)
    when = m.group(1) if m else captured_at
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(script)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope_systems: Dict[str, Set[str]] = {}
    if "snapshot_scope" in tables:
        for r in con.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
            scope_systems.setdefault(r["origin_system"], set())
            when = r["captured_at"] or when
    for table in tables:
        if table == "snapshot_scope":
            continue
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
        replica = None
        base = table
        for suffix in ("_failover_replica", "_primary", "_read_replica", "_reporting_replica", "_replica_ap_south", "_replica_eu_west"):
            if table.endswith(suffix):
                replica = suffix.lstrip("_")
                base = table[: -len(suffix)]
                break
        for i, row in enumerate(rows):
            src = f"{path.name}:{table}[{i}]"
            if "record_json" in row:  # generic customer_data_* tables
                rec = json.loads(row["record_json"])
                kind = COLLECTION_KIND.get(row.get("collection_name", ""), "unknown")
                obs = record_observation(rec, kind, evidence_id, row.get("updated_at") or when, src)
                if obs:
                    obs.replica = replica or table
                    parsed.observations.append(obs)
                    scope_systems.setdefault(row.get("origin_system", obs.system_id), set()).add(obs.resource_id)
                continue
            kind = TABLE_KIND.get(base, TABLE_KIND.get(table, "unknown"))
            if kind == "unknown":
                continue
            rec = dict(row)
            if "customer_reference" not in rec and "user_id" in rec:
                rec["customer_reference"] = rec["user_id"]
            obs = record_observation(rec, kind, evidence_id, when, src)
            if obs:
                obs.replica = replica or table
                parsed.observations.append(obs)
                system = TABLE_SYSTEM.get(base, obs.system_id)
                scope_systems.setdefault(system, set()).add(obs.resource_id)
    con.close()
    for system, present in scope_systems.items():
        parsed.scopes.append(Scope(system_id=system, evidence_id=evidence_id, observed_at=when, present=present))
    return parsed
