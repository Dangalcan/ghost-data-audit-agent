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

from dataclasses import dataclass
from typing import List, Optional

from .state import ResourceState

DELETE_KINDS = {"profile", "search_entry", "export", "support_attachment", "object", "marketing_profile",
                "cache_entry", "support_contact", "analytics_contact", "unknown"}
REDACT_KINDS = {"support_message", "tombstone", "email_delivery_log"}
ANONYMIZE_KINDS = {"analytics_profile"}
RETAIN_KINDS = {"audit_record", "legal_hold"}

KIND_TO_CATEGORY = {
    "profile": "USER_PROFILE", "search_entry": "SEARCH_INDEX_ENTRY", "export": "USER_EXPORT",
    "support_attachment": "SUPPORT_ATTACHMENT", "object": "SUPPORT_ATTACHMENT", "marketing_profile": "MARKETING_PROFILE",
    "cache_entry": "CACHE_ENTRY", "support_contact": "SECONDARY_IDENTIFIER", "analytics_contact": "SECONDARY_IDENTIFIER",
    "support_message": "SUPPORT_MESSAGE", "tombstone": "ACCOUNT_TOMBSTONE", "email_delivery_log": "EMAIL_DELIVERY_LOG",
    "analytics_profile": "ANALYTICS_PROFILE", "connector": "DATA_FLOW", "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG", "legal_hold": "LEGAL_HOLD_RECORD", "unknown": "USER_PROFILE",
}
CATEGORY_TO_RULE = {
    "USER_PROFILE": "rule_01", "SECONDARY_IDENTIFIER": "rule_02", "SEARCH_INDEX_ENTRY": "rule_03", "CACHE_ENTRY": "rule_04",
    "SUPPORT_MESSAGE": "rule_05", "SUPPORT_ATTACHMENT": "rule_06", "ANALYTICS_PROFILE": "rule_07", "PRODUCT_TELEMETRY": "rule_08",
    "USER_EXPORT": "rule_09", "MARKETING_PROFILE": "rule_10", "EMAIL_DELIVERY_LOG": "rule_11", "SECURITY_AUDIT_LOG": "rule_12",
    "LEGAL_HOLD_RECORD": "rule_13", "BACKUP_ARCHIVE": "rule_14", "ACCOUNT_TOMBSTONE": "rule_15", "DATA_FLOW": "rule_14",
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
        return {"step_no": step_no, "phase": self.phase, "system_id": self.system_id, "resource_id": self.resource_id,
                "operation": self.operation, "state": self.state, "evidence_id": self.evidence_id}


@dataclass
class ResourcePlan:
    resource_id: str
    kind: str
    category: str
    rule_id: str
    findings: List[Step]
    remediation: List[Step]
    verification: List[Step]
    proof_evidence: Optional[str]   # evidence proving the compliant state (COMPLETE citations)
    reason: str


def plan_resource(st: ResourceState, fallback_evidence: Optional[str] = None) -> ResourcePlan:
    """fallback_evidence: activity_id to cite when no evidence shows the compliant state."""
    kind, sysid, rid = st.kind, st.system_id, st.resource_id
    category = KIND_TO_CATEGORY.get(kind, "USER_PROFILE")
    rule = CATEGORY_TO_RULE.get(category, "")
    F, R, V = [], [], []
    proof = st.evidence_id if st.presence != "UNKNOWN" else None
    reason = f"{kind} -> {category} ({rule}); state at audit: {st.presence}"
    ev = st.evidence_id or ""

    if kind in RETAIN_KINDS:
        V.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_PRESENT", "PRESENT", rule_id=rule))
        proof = st.evidence_id if st.presence == "PRESENT" else fallback_evidence
        reason += "; retained by policy"
    elif kind == "connector":
        if st.enabled is True:
            F.append(Step("FINDING", sysid, rid, "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", ev, rule, "connector enabled"))
            R.append(Step("REMEDIATION", sysid, rid, "DISABLE_DATA_FLOW", "DISABLED", rule_id=rule))
            proof = None
        elif st.enabled is None and st.presence == "UNKNOWN":
            proof = fallback_evidence
        V.append(Step("VERIFICATION", sysid, rid, "VERIFY_DATA_FLOW_DISABLED", "DISABLED", rule_id=rule))
    elif kind == "backup":
        if st.restore_allowed is True:
            F.append(Step("FINDING", sysid, rid, "REPORT_RESTORE_RISK", "ACTIVE", ev, rule, "restore still allowed"))
            R.append(Step("REMEDIATION", sysid, rid, "BLOCK_RESTORE", "BLOCKED", rule_id=rule))
            proof = None
        elif st.restore_allowed is None and st.presence == "UNKNOWN":
            proof = fallback_evidence
        V.append(Step("VERIFICATION", sysid, rid, "VERIFY_RESTORE_BLOCKED", "BLOCKED", rule_id=rule))
    elif kind in DELETE_KINDS:
        if st.presence == "PRESENT":
            F.append(Step("FINDING", sysid, rid, "REPORT_REMAINING_RECORD", "PRESENT", ev, rule, "record still exists"))
            R.append(Step("REMEDIATION", sysid, rid, "DELETE_RECORD", "ABSENT", rule_id=rule))
            proof = None
        elif st.presence == "UNKNOWN":
            proof = fallback_evidence
        V.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_ABSENT", "ABSENT", rule_id=rule))
    elif kind in REDACT_KINDS or kind in ANONYMIZE_KINDS:
        redact = kind in REDACT_KINDS
        op, final = ("REDACT_RECORD", "REDACTED") if redact else ("ANONYMIZE_RECORD", "ANONYMIZED")
        vop = "VERIFY_RECORD_REDACTED" if redact else "VERIFY_RECORD_ANONYMIZED"
        if st.presence == "PRESENT" and st.pii is True:
            F.append(Step("FINDING", sysid, rid, "REPORT_EXPOSED_PII", "PRESENT", ev, rule, "direct identifiers still exposed"))
            R.append(Step("REMEDIATION", sysid, rid, op, final, rule_id=rule))
            proof = None
            V.append(Step("VERIFICATION", sysid, rid, vop, final, rule_id=rule))
        elif st.presence == "ABSENT":
            V.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_ABSENT", "ABSENT", rule_id=rule))
        else:
            if st.presence == "UNKNOWN":
                proof = fallback_evidence
            V.append(Step("VERIFICATION", sysid, rid, vop, final, rule_id=rule))
    else:
        V.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_ABSENT", "ABSENT", rule_id=rule))
    return ResourcePlan(rid, kind, category, rule, F, R, V, proof, reason)
