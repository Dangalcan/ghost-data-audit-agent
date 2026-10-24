"""State of one resource at audit_at, from evidence only.

Rule: the latest grounded observation at or before audit_at wins. Ties at the same
instant (e.g. primary vs replica rows in one snapshot) resolve towards PRESENT and
towards "identifiers still exposed": a copy that still exists anywhere counts.
Absence may also be inferred from a scoped snapshot that is complete for the
resource's system but does not contain it (never for retained security / legal records).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .evidence.base import Observation, Scope, default_kind

KIND_PRIORITY = {  # more specific kinds win when sources disagree
    "support_attachment": 3, "support_contact": 3, "analytics_contact": 3, "object": 3,
    "tombstone": 2, "legal_hold": 2, "audit_record": 2, "connector": 2, "backup": 2, "export": 2,
    "profile": 1, "search_entry": 1, "analytics_profile": 1, "support_message": 1, "marketing_profile": 1,
    "unknown": 0,
}
NO_IMPLICIT_ABSENCE = {"audit_record", "legal_hold"}


@dataclass
class ResourceState:
    resource_id: str
    kind: str
    presence: str                    # PRESENT | ABSENT | UNKNOWN
    pii: Optional[bool]
    enabled: Optional[bool]
    restore_allowed: Optional[bool]
    retention_until: Optional[str]
    evidence_id: Optional[str]       # evidence proving this state (None when UNKNOWN)
    observed_at: Optional[str]
    chosen: Optional[Observation]
    history: List[Observation]

    @property
    def system_id(self) -> str:
        from .config import system_for_resource
        return system_for_resource(self.resource_id)


def resolve_kind(observations: Iterable[Observation], resource_id: str) -> str:
    best, score = default_kind(resource_id), -1
    for o in observations:
        s = KIND_PRIORITY.get(o.kind, 0)
        if s > score:
            best, score = o.kind, s
    return best


def _rank(o: Observation):
    # later first; at the same instant: explicit over implicit, PRESENT over ABSENT, exposed over clean
    return (o.observed_at, 0 if o.implicit else 1, 1 if o.presence == "PRESENT" else 0, 1 if o.pii else 0,
            1 if o.enabled else 0, 1 if o.restore_allowed else 0)


def state_at(resource_id: str, audit_at: str, observations: List[Observation], scopes: List[Scope]) -> ResourceState:
    kind = resolve_kind(observations, resource_id)
    hist = [o for o in observations if o.resource_id == resource_id and o.usable_at(audit_at)]
    if kind not in NO_IMPLICIT_ABSENCE:
        from .config import system_for_resource
        system = system_for_resource(resource_id)
        for sc in scopes:
            if sc.system_id == system and sc.usable_at(audit_at) and resource_id not in sc.present:
                hist.append(Observation(resource_id=resource_id, evidence_id=sc.evidence_id, observed_at=sc.observed_at,
                                        presence="ABSENT", kind=kind, tokens=observations[0].tokens if observations else None,
                                        implicit=True, source=f"scope:{sc.evidence_id}:{system}", captured_at=sc.captured_at))
    hist.sort(key=_rank, reverse=True)
    if not hist:
        return ResourceState(resource_id, kind, "UNKNOWN", None, None, None, None, None, None, None, [])
    top = hist[0]
    # merge flags from every observation at the winning instant (replica drift, multi-row snapshots)
    same = [o for o in hist if o.observed_at == top.observed_at and not o.implicit] or [top]
    presence = "PRESENT" if any(o.presence == "PRESENT" for o in same) else top.presence
    pii = None
    if any(o.pii is True for o in same):
        pii = True
    elif any(o.pii is False for o in same):
        pii = False
    enabled = True if any(o.enabled is True for o in same) else (False if any(o.enabled is False for o in same) else None)
    restore = True if any(o.restore_allowed is True for o in same) else (False if any(o.restore_allowed is False for o in same) else None)
    retention = next((o.retention_until for o in same if o.retention_until), None) or next((o.retention_until for o in hist if o.retention_until), None)
    chosen = next((o for o in same if o.presence == presence and (pii is None or o.pii == pii)), top)
    return ResourceState(resource_id, kind, presence, pii, enabled, restore, retention, chosen.evidence_id,
                         chosen.observed_at, chosen, hist)
