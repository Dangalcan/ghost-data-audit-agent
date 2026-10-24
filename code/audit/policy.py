"""deletion_rules.csv applied to one resource state -> required steps.

Category mapping (record kind -> rule):
  profile, search_entry, export, support_attachment, object, marketing_profile,
  cache_entry, support_contact, analytics_contact (hash-only contact entry =
  SECONDARY_IDENTIFIER)                                  -> DELETE
  support_message (shared conversation context)           -> REDACT
  analytics_profile (still attributable)                  -> ANONYMIZE
  tombstone (ACCOUNT_TOMBSTONE)                           -> REDACT (keep user_id only)
  email_delivery_log                                      -> REDACT
  connector (active data flow)                            -> DISABLE
  backup (restore access can be blocked)                  -> BLOCK_RESTORE, record retained
  audit_record (within retention), legal_hold             -> RETAIN
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .state import ResourceState

DELETE_KINDS = {
    "profile",
    "search_entry",
    "export",
    "support_attachment",
    "object",
    "marketing_profile",
    "cache_entry",
    "support_contact",
    "analytics_contact",
    "unknown",
}
REDACT_KINDS = {"support_message", "tombstone", "email_delivery_log"}
ANONYMIZE_KINDS = {"analytics_profile"}
RETAIN_KINDS = {"audit_record", "legal_hold"}

KIND_TO_CATEGORY = {
    "profile": "USER_PROFILE",
    "search_entry": "SEARCH_INDEX_ENTRY",
    "export": "USER_EXPORT",
    "support_attachment": "SUPPORT_ATTACHMENT",
    "object": "SUPPORT_ATTACHMENT",
    "marketing_profile": "MARKETING_PROFILE",
    "cache_entry": "CACHE_ENTRY",
    "support_contact": "SECONDARY_IDENTIFIER",
    "analytics_contact": "SECONDARY_IDENTIFIER",
    "support_message": "SUPPORT_MESSAGE",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "email_delivery_log": "EMAIL_DELIVERY_LOG",
    "analytics_profile": "ANALYTICS_PROFILE",
    "connector": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "unknown": "USER_PROFILE",
}
CATEGORY_TO_RULE = {
    "USER_PROFILE": "rule_01",
    "SECONDARY_IDENTIFIER": "rule_02",
    "SEARCH_INDEX_ENTRY": "rule_03",
    "CACHE_ENTRY": "rule_04",
    "SUPPORT_MESSAGE": "rule_05",
    "SUPPORT_ATTACHMENT": "rule_06",
    "ANALYTICS_PROFILE": "rule_07",
    "PRODUCT_TELEMETRY": "rule_08",
    "USER_EXPORT": "rule_09",
    "MARKETING_PROFILE": "rule_10",
    "EMAIL_DELIVERY_LOG": "rule_11",
    "SECURITY_AUDIT_LOG": "rule_12",
    "LEGAL_HOLD_RECORD": "rule_13",
    "BACKUP_ARCHIVE": "rule_14",
    "ACCOUNT_TOMBSTONE": "rule_15",
    "DATA_FLOW": "rule_14",
}


@dataclass
class Step:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str = ""
    rule_id: str = ""
    reason: str = ""

    def as_json(self, step_no: int) -> dict:
        return {
            "step_no": step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


@dataclass
class ResourcePlan:
    resource_id: str
    kind: str
    category: str
    rule_id: str
    findings: list[Step] = field(default_factory=list)
    remediation: list[Step] = field(default_factory=list)
    verification: list[Step] = field(default_factory=list)
    proof_evidence: str | None = None  # evidence proving the compliant state (COMPLETE citations)
    reason: str = ""

    def finding(self, operation: str, state: str, evidence: str, note: str) -> None:
        sysid = _system(self.resource_id)
        self.findings.append(Step("FINDING", sysid, self.resource_id, operation, state, evidence, self.rule_id, note))
        self.proof_evidence = None

    def remediate(self, operation: str, state: str) -> None:
        self.remediation.append(
            Step("REMEDIATION", _system(self.resource_id), self.resource_id, operation, state, rule_id=self.rule_id)
        )

    def verify(self, operation: str, state: str) -> None:
        self.verification.append(
            Step("VERIFICATION", _system(self.resource_id), self.resource_id, operation, state, rule_id=self.rule_id)
        )


def _system(resource_id: str) -> str:
    from .config import system_for_resource

    return system_for_resource(resource_id)


# --------------------------------------------------------------------------- per-category rules
def _plan_retained(st: ResourceState, plan: ResourcePlan, fallback: str | None) -> None:
    plan.verify("VERIFY_RECORD_PRESENT", "PRESENT")
    plan.proof_evidence = st.evidence_id if st.presence == "PRESENT" else fallback
    plan.reason += "; retained by policy"


def _plan_connector(st: ResourceState, plan: ResourcePlan, fallback: str | None) -> None:
    if st.enabled is True:
        plan.finding("REPORT_ACTIVE_DATA_FLOW", "ACTIVE", st.evidence_id or "", "connector enabled")
        plan.remediate("DISABLE_DATA_FLOW", "DISABLED")
    elif st.enabled is None and st.presence == "UNKNOWN":
        plan.proof_evidence = fallback
    plan.verify("VERIFY_DATA_FLOW_DISABLED", "DISABLED")


def _plan_backup(st: ResourceState, plan: ResourcePlan, fallback: str | None) -> None:
    if st.restore_allowed is True:
        plan.finding("REPORT_RESTORE_RISK", "ACTIVE", st.evidence_id or "", "restore still allowed")
        plan.remediate("BLOCK_RESTORE", "BLOCKED")
    elif st.restore_allowed is None and st.presence == "UNKNOWN":
        plan.proof_evidence = fallback
    plan.verify("VERIFY_RESTORE_BLOCKED", "BLOCKED")


def _plan_delete(st: ResourceState, plan: ResourcePlan, fallback: str | None) -> None:
    if st.presence == "PRESENT":
        plan.finding("REPORT_REMAINING_RECORD", "PRESENT", st.evidence_id or "", "record still exists")
        plan.remediate("DELETE_RECORD", "ABSENT")
    elif st.presence == "UNKNOWN":
        plan.proof_evidence = fallback
    plan.verify("VERIFY_RECORD_ABSENT", "ABSENT")


def _plan_redact_or_anonymise(st: ResourceState, plan: ResourcePlan, fallback: str | None) -> None:
    redact = st.kind in REDACT_KINDS
    op, final = ("REDACT_RECORD", "REDACTED") if redact else ("ANONYMIZE_RECORD", "ANONYMIZED")
    vop = "VERIFY_RECORD_REDACTED" if redact else "VERIFY_RECORD_ANONYMIZED"
    if st.presence == "PRESENT" and st.pii is True:
        plan.finding("REPORT_EXPOSED_PII", "PRESENT", st.evidence_id or "", "direct identifiers still exposed")
        plan.remediate(op, final)
        plan.verify(vop, final)
        return
    if st.presence == "ABSENT":
        plan.verify("VERIFY_RECORD_ABSENT", "ABSENT")
        return
    if st.presence == "UNKNOWN":
        plan.proof_evidence = fallback
    plan.verify(vop, final)


def plan_resource(st: ResourceState, fallback_evidence: str | None = None) -> ResourcePlan:
    """Required steps for one resource. `fallback_evidence`: activity id cited when no file
    shows the compliant state (never a FAILED or PARTIAL claim, see planner.fallback_activity)."""
    kind = st.kind
    category = KIND_TO_CATEGORY.get(kind, "USER_PROFILE")
    plan = ResourcePlan(
        resource_id=st.resource_id,
        kind=kind,
        category=category,
        rule_id=CATEGORY_TO_RULE.get(category, ""),
        proof_evidence=st.evidence_id if st.presence != "UNKNOWN" else None,
        reason=f"{kind} -> {category} ({CATEGORY_TO_RULE.get(category, '')}); state at audit: {st.presence}",
    )
    if kind in RETAIN_KINDS:
        _plan_retained(st, plan, fallback_evidence)
    elif kind == "connector":
        _plan_connector(st, plan, fallback_evidence)
    elif kind == "backup":
        _plan_backup(st, plan, fallback_evidence)
    elif kind in DELETE_KINDS:
        _plan_delete(st, plan, fallback_evidence)
    elif kind in REDACT_KINDS or kind in ANONYMIZE_KINDS:
        _plan_redact_or_anonymise(st, plan, fallback_evidence)
    else:
        plan.verify("VERIFY_RECORD_ABSENT", "ABSENT")
    return plan
