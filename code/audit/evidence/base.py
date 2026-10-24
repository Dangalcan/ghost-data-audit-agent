"""Common observation model produced by every evidence parser.

An Observation is one *grounded* statement about one resource at one time:
"evidence_X shows resource R in system S at time T with presence P and these
identifiers". Everything downstream (linkage, state, policy) works on this model,
so the format zoo in dataset/evidence/ is isolated here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..config import system_for_resource
from ..identity import Tokens, tokens_from_values

# record kinds -> policy category is decided in policy.py; kinds keep the evidence vocabulary.
KIND_BY_RECORD_TYPE = {
    "USER_PROFILE": "profile",
    "SEARCH_INDEX_ENTRY": "search_entry",
    "ANALYTICS_PROFILE": "analytics_profile",
    "SUPPORT_MESSAGE": "support_message",
    "SUPPORT_ATTACHMENT": "support_attachment",
    "USER_EXPORT": "export",
    "DATA_FLOW": "connector",
    "BACKUP_ARCHIVE": "backup",
    "SECURITY_AUDIT_LOG": "audit_record",
    "LEGAL_HOLD_RECORD": "legal_hold",
    "ACCOUNT_TOMBSTONE": "tombstone",
    "MARKETING_PROFILE": "marketing_profile",
    "CACHE_ENTRY": "cache_entry",
    "EMAIL_DELIVERY_LOG": "email_delivery_log",
}
DEFAULT_KIND_BY_PREFIX = {
    "account": "profile", "search": "search_entry", "analytics": "analytics_profile",
    "support": "support_message", "export": "export", "data_flow": "connector", "backup": "backup",
    "audit_record": "audit_record", "tombstone": "tombstone", "legal_hold": "legal_hold",
    "marketing": "marketing_profile", "object": "object", "cache": "cache_entry",
    "email_delivery": "email_delivery_log",
}


def default_kind(resource_id: str) -> str:
    return DEFAULT_KIND_BY_PREFIX.get(resource_id.rsplit("_", 1)[0], "unknown")


@dataclass
class Observation:
    resource_id: str
    evidence_id: str
    observed_at: str                       # ISO-8601 UTC
    presence: str                          # PRESENT | ABSENT | UNKNOWN
    kind: str                              # see KIND_BY_RECORD_TYPE values
    tokens: Tokens                         # identifiers found in / joined to this record
    pii: Optional[bool] = None             # direct identifiers (name/email/phone/hash) present?
    enabled: Optional[bool] = None         # connectors
    restore_allowed: Optional[bool] = None  # backups
    retention_until: Optional[str] = None  # audit / legal records
    replica: Optional[str] = None          # table / replica the copy was seen in
    implicit: bool = False                 # absence inferred from a scoped snapshot
    source: str = ""                       # file:line or json path (for --explain)
    extra: Dict[str, object] = field(default_factory=dict)
    captured_at: str = ""                  # when the *file* was captured (set by the registry)

    def usable_at(self, audit_at: str) -> bool:
        """Could an auditor have seen this at audit_at? Both the file and the record must predate it."""
        return self.observed_at <= audit_at and (not self.captured_at or self.captured_at <= audit_at)

    @property
    def system_id(self) -> str:
        return system_for_resource(self.resource_id)

    def short(self) -> str:
        flags = []
        if self.pii is not None:
            flags.append(f"pii={self.pii}")
        if self.enabled is not None:
            flags.append(f"enabled={self.enabled}")
        if self.restore_allowed is not None:
            flags.append(f"restore_allowed={self.restore_allowed}")
        if self.retention_until:
            flags.append(f"retention_until={self.retention_until}")
        if self.replica:
            flags.append(f"replica={self.replica}")
        if self.implicit:
            flags.append("implicit")
        return (f"{self.observed_at} {self.evidence_id:12} {self.resource_id:18} {self.presence:8} {self.kind:18} "
                + " ".join(flags) + f" ids={sorted(self.tokens.all)}")


@dataclass
class Scope:
    """A snapshot that is complete for one system at one time: resources of that
    system which are absent from `present` were absent at `observed_at`."""
    system_id: str
    evidence_id: str
    observed_at: str
    present: Set[str]
    captured_at: str = ""

    def usable_at(self, audit_at: str) -> bool:
        return self.observed_at <= audit_at and (not self.captured_at or self.captured_at <= audit_at)


@dataclass
class ParsedEvidence:
    evidence_id: str
    captured_at: str
    observations: List[Observation] = field(default_factory=list)
    scopes: List[Scope] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def resource_ids(self) -> Set[str]:
        return {o.resource_id for o in self.observations}


def to_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().strip('"').lower()
    if s in ("1", "true", "yes", "enabled", "active", "allowed"):
        return True
    if s in ("0", "false", "no", "disabled", "stopped", "blocked", "inactive"):
        return False
    return None


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().strip('"').lower() in ("", "null", "none", "-", "—"))


def presence_from_result(result: str) -> str:
    r = (result or "").upper()
    if r in ("NOT_FOUND", "ABSENT", "MISS", "DELETED"):
        return "ABSENT"
    if r in ("FOUND", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED", "PRESENT", "HIT"):
        return "PRESENT"
    return "UNKNOWN"


def flags_from_result(kind: str, result: str, subject: Optional[str] = None) -> dict:
    """Map the ledger-style result vocabulary to pii / enabled / restore_allowed.

    `subject` is the record's subject reference: a direct identifier (email / phone)
    means identifiers are still exposed; a bare user_id means only the stable
    reference remains.
    """
    r = (result or "").upper()
    out: dict = {}
    if r == "FOUND" and kind in ("support_message", "analytics_profile", "tombstone", "email_delivery_log") and subject:
        sub = str(subject).strip().lower()
        out["pii"] = not sub.startswith("user_")
    if kind == "connector":
        out["enabled"] = True if r == "ACTIVE" else False if r == "DISABLED" else None
    elif kind == "backup":
        out["restore_allowed"] = True if r == "ACTIVE" else False if r == "BLOCKED" else None
    elif r == "IDENTIFIABLE":
        out["pii"] = True
    elif r in ("ANONYMIZED", "REDACTED"):
        out["pii"] = False
    elif r == "FOUND" and kind in ("profile", "search_entry", "export", "support_attachment", "object", "marketing_profile"):
        out["pii"] = True
    return out


def tokens_of(*values) -> Tokens:
    return tokens_from_values(values)
