"""Additional evaluation cases written by the team (required by the challenge).

Each case feeds synthetic observations (what the evidence parsers would produce)
through the real decision core - state_at -> plan_resource -> ordering - and checks
the exact ordered steps. They cover situations the public sample does not isolate:

  C1  historical email + hash-only analytics contact (SECONDARY_IDENTIFIER -> DELETE)
  C2  replication drift: primary says deleted, failover replica still holds the profile
  C3  non-deletion request that contains deletion vocabulary -> NOT_APPLICABLE
  C4  tombstone still exposing name/email -> REDACT, audit record retained
  C5  evidence captured after audit_at must not change the verdict
  C6  COMPLETE case cites the evidence proving each compliant state

Run: python code/evaluation/custom_cases.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.evidence.base import Observation, Scope, default_kind  # noqa: E402
from audit.identity import tokens_from_values  # noqa: E402
from audit.intent import guard  # noqa: E402
from audit.planner import step_sort_key  # noqa: E402
from audit.policy import plan_resource  # noqa: E402
from audit.state import state_at  # noqa: E402

AUDIT_AT = "2026-09-15T12:00:00Z"


def O(rid, when, presence="PRESENT", ev="evidence_x", captured=None, kind=None, **kw):
    return Observation(resource_id=rid, evidence_id=ev, observed_at=when, presence=presence,
                       kind=kind or default_kind(rid), tokens=tokens_from_values([]), captured_at=captured or when, **kw)


def decide(observations, scopes=(), audit_at=AUDIT_AT):
    """Mini planner: same decision core as audit.planner.audit_case, synthetic evidence."""
    by_rid = {}
    for o in observations:
        by_rid.setdefault(o.resource_id, []).append(o)
    plans = [plan_resource(state_at(rid, audit_at, obs, list(scopes))) for rid, obs in by_rid.items()]
    findings = [s for p in plans for s in p.findings]
    if findings:
        steps = findings + [s for p in plans for s in p.remediation] + [s for p in plans for s in p.verification]
        for s in steps:
            if s.phase != "FINDING":
                s.evidence_id = ""
        status = "INCOMPLETE"
    else:
        steps = []
        for p in plans:
            for s in p.verification:
                if p.proof_evidence:
                    s.evidence_id = p.proof_evidence
                    steps.append(s)
        status = "COMPLETE"
    steps.sort(key=step_sort_key)
    return status, [(s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id) for s in steps]


CASES = []


def case(name, expected_status, expected_steps, build):
    CASES.append((name, expected_status, expected_steps, build))


# C1 - historical email: the analytics platform still keys a contact entry by the hash of
# the OLD email address. Policy: SECONDARY_IDENTIFIER -> DELETE (not ANONYMIZE).
case("C1 historical email hash-only analytics contact", "INCOMPLETE", [
    ("FINDING", "analytics_platform", "analytics_9914", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01"),
    ("REMEDIATION", "analytics_platform", "analytics_9914", "DELETE_RECORD", "ABSENT", ""),
    ("VERIFICATION", "account_service", "account_9797", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
    ("VERIFICATION", "analytics_platform", "analytics_9914", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
    ("VERIFICATION", "security_service", "audit_record_2945", "VERIFY_RECORD_PRESENT", "PRESENT", ""),
], lambda: decide([
    O("account_9797", "2026-09-15T11:56:57Z", "ABSENT", "evidence_01"),
    O("analytics_9914", "2026-09-15T11:58:26Z", "PRESENT", "evidence_01", kind="analytics_contact", pii=True),
    O("audit_record_2945", "2026-09-15T11:55:40Z", "PRESENT", "evidence_01", retention_until="2034-09-30T23:59:59Z"),
]))

# C2 - replication drift: the primary table no longer holds the profile but the failover
# replica (same capture) still does -> the record still exists.
case("C2 replication drift keeps the profile alive", "INCOMPLETE", [
    ("FINDING", "account_service", "account_005", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_54"),
    ("REMEDIATION", "account_service", "account_005", "DELETE_RECORD", "ABSENT", ""),
    ("VERIFICATION", "account_service", "account_005", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
], lambda: decide([
    O("account_005", "2026-09-02T23:00:00Z", "ABSENT", "evidence_54", replica="primary"),
    O("account_005", "2026-09-02T23:00:00Z", "PRESENT", "evidence_54", replica="failover_replica", pii=True),
], audit_at="2026-09-08T13:40:00Z"))

# C3 - non-deletion request that mentions deletion -> NOT_APPLICABLE, no steps.
case("C3 non-deletion request with deletion vocabulary", "NOT_APPLICABLE", [], lambda: (
    "NOT_APPLICABLE" if guard("Please do not delete anything. I only want a copy of the information held on my account; I still use it.")["label"] == "NON_DELETION" else "COMPLETE", []))

# C4 - tombstone still carries name/email; audit record retained; account gone.
case("C4 tombstone exposing identifiers", "INCOMPLETE", [
    ("FINDING", "identity_service", "tombstone_9178", "REPORT_EXPOSED_PII", "PRESENT", "evidence_51"),
    ("REMEDIATION", "identity_service", "tombstone_9178", "REDACT_RECORD", "REDACTED", ""),
    ("VERIFICATION", "account_service", "account_8858", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
    ("VERIFICATION", "security_service", "audit_record_7068", "VERIFY_RECORD_PRESENT", "PRESENT", ""),
    ("VERIFICATION", "identity_service", "tombstone_9178", "VERIFY_RECORD_REDACTED", "REDACTED", ""),
], lambda: decide([
    O("tombstone_9178", "2026-09-15T12:00:00Z", "PRESENT", "evidence_51", pii=True),
    O("audit_record_7068", "2026-09-15T12:00:00Z", "PRESENT", "evidence_51"),
], scopes=[Scope("account_service", "evidence_51", "2026-09-15T12:00:00Z", {"account_8858x"}, "2026-09-15T12:00:00Z")]
   if False else [Scope("account_service", "evidence_51", "2026-09-15T12:00:00Z", set(), "2026-09-15T12:00:00Z")]) if False else decide([
    O("account_8858", "2026-09-15T12:00:00Z", "ABSENT", "evidence_51", implicit=True),
    O("tombstone_9178", "2026-09-15T12:00:00Z", "PRESENT", "evidence_51", pii=True),
    O("audit_record_7068", "2026-09-15T12:00:00Z", "PRESENT", "evidence_51"),
]))

# C5 - a later capture (file captured after audit_at) shows the search entry deleted; the
# auditor could not have seen it, so the entry is still PRESENT at audit_at.
case("C5 evidence captured after audit_at is ignored", "INCOMPLETE", [
    ("FINDING", "search_service", "search_4013", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_94"),
    ("REMEDIATION", "search_service", "search_4013", "DELETE_RECORD", "ABSENT", ""),
    ("VERIFICATION", "search_service", "search_4013", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
], lambda: decide([
    O("search_4013", "2026-09-26T09:00:00Z", "PRESENT", "evidence_94", captured="2026-09-26T10:38:56Z", pii=True),
    O("search_4013", "2026-09-26T10:50:00Z", "ABSENT", "evidence_99", captured="2026-09-28T00:00:00Z"),
], audit_at="2026-09-26T11:00:00Z"))

# C6 - everything compliant: verification steps cite the proving evidence, in table order.
case("C6 complete deletion cites proof per record", "COMPLETE", [
    ("VERIFICATION", "account_service", "account_5684", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_92"),
    ("VERIFICATION", "security_service", "audit_record_5416", "VERIFY_RECORD_PRESENT", "PRESENT", "evidence_92"),
    ("VERIFICATION", "support_platform", "support_4715", "VERIFY_RECORD_REDACTED", "REDACTED", "evidence_92"),
    ("VERIFICATION", "analytics_platform", "analytics_4566", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", "evidence_92"),
    ("VERIFICATION", "integration_service", "data_flow_6244", "VERIFY_DATA_FLOW_DISABLED", "DISABLED", "evidence_92"),
    ("VERIFICATION", "backup_service", "backup_3120", "VERIFY_RESTORE_BLOCKED", "BLOCKED", "evidence_92"),
], lambda: decide([
    O("account_5684", "2026-09-15T09:01:48Z", "ABSENT", "evidence_92"),
    O("audit_record_5416", "2026-09-15T09:30:00Z", "PRESENT", "evidence_92", retention_until="2034-09-30T23:59:59Z"),
    O("support_4715", "2026-09-15T10:00:00Z", "PRESENT", "evidence_92", pii=False),
    O("analytics_4566", "2026-09-15T11:29:16Z", "PRESENT", "evidence_92", pii=False),
    O("data_flow_6244", "2026-09-15T10:10:00Z", "PRESENT", "evidence_92", enabled=False),
    O("backup_3120", "2026-09-15T10:58:35Z", "PRESENT", "evidence_92", restore_allowed=False),
]))


def run() -> int:
    failures = 0
    for name, exp_status, exp_steps, build in CASES:
        status, steps = build()
        ok = status == exp_status and steps == exp_steps
        print(f"{'PASS' if ok else 'FAIL'}  {name}: status={status} steps={len(steps)}")
        if not ok:
            failures += 1
            print("   expected:", exp_status, json.dumps(exp_steps, indent=1))
            print("   got:     ", status, json.dumps(steps, indent=1))
    print(f"{len(CASES) - failures}/{len(CASES)} custom cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
