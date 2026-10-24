"""Per-case audit: identity -> linked resources -> state -> policy -> ordered steps."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Set

from . import evidence as evidence_mod
from .activity import activities_by_resource
from .config import OPERATION_ORDER, PHASE_ORDER
from .identity import Subject, owners_of, resolve_subject
from .intent import classify_intent
from .linkage import Link, linked_resources, observations_by_resource
from .loaders import CaseRequest
from .policy import ResourcePlan, Step, plan_resource
from .state import ResourceState, state_at

POLICY_SUCCESS_EVENTS = {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "RETAIN_RECORD"}


@dataclass
class CaseResult:
    case_id: str
    deletion_status: str
    steps: List[Step]
    subject: Optional[Subject] = None
    intent: Optional[dict] = None
    links: Dict[str, Link] = field(default_factory=dict)
    states: Dict[str, ResourceState] = field(default_factory=dict)
    plans: Dict[str, ResourcePlan] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def steps_json(self) -> str:
        return json.dumps([s.as_json(i + 1) for i, s in enumerate(self.steps)], separators=(",", ":"))


def step_sort_key(step: Step):
    return (PHASE_ORDER.index(step.phase), OPERATION_ORDER.index(step.operation), step.system_id, step.resource_id)


def fallback_activity(resource_id: str, audit_at: str) -> Optional[str]:
    """Latest successful policy action on the resource at or before audit_at (a claim, last resort)."""
    best = None
    for a in activities_by_resource().get(resource_id, []):
        if a.event_time <= audit_at and a.event_type in POLICY_SUCCESS_EVENTS and a.reported_result == "SUCCESS":
            best = a.activity_id
    return best


def audit_case(case: CaseRequest, skip_images: bool = False, use_llm: bool = True) -> CaseResult:
    intent = classify_intent(case, use_llm=use_llm)
    if intent["label"] != "DELETION":
        return CaseResult(case.case_id, "NOT_APPLICABLE", [], None, intent, notes=[intent.get("reason", "")])
    subject = resolve_subject(case.email)
    if subject is None:
        return CaseResult(case.case_id, "COMPLETE", [], None, intent, notes=["email matches no identity"])
    links = linked_resources(case, subject, skip_images)
    usable_scopes = evidence_mod.scopes_for(subject.user_id, skip_images)
    obs_by_rid = observations_by_resource(skip_images)
    states: Dict[str, ResourceState] = {}
    plans: Dict[str, ResourcePlan] = {}
    for rid in links:
        st = state_at(rid, case.audit_at, obs_by_rid.get(rid, []), usable_scopes)
        states[rid] = st
        plans[rid] = plan_resource(st, fallback_activity(rid, case.audit_at))
    findings = [s for p in plans.values() for s in p.findings]
    if findings:
        status = "INCOMPLETE"
        steps = findings + [s for p in plans.values() for s in p.remediation] + [s for p in plans.values() for s in p.verification]
        for s in steps:
            if s.phase != "FINDING":
                s.evidence_id = ""
    else:
        status = "COMPLETE"
        steps = []
        for p in plans.values():
            for s in p.verification:
                if p.proof_evidence:
                    s.evidence_id = p.proof_evidence
                    steps.append(s)
    steps.sort(key=step_sort_key)
    return CaseResult(case.case_id, status, steps, subject, intent, links, states, plans)


def explain(result: CaseResult) -> str:
    lines = [f"{result.case_id}: {result.deletion_status}"]
    if result.intent:
        lines.append(f"  intent: {result.intent}")
    if result.subject:
        s = result.subject
        lines.append(f"  subject: {s.user_id} {s.name} emails={sorted(s.emails)} phones={sorted(s.phones)}")
    for rid, link in sorted(result.links.items()):
        st = result.states[rid]
        p = result.plans[rid]
        lines.append(f"  - {rid} [{link.confidence}] {p.reason}; proof={p.proof_evidence}")
        for o in st.history[:4]:
            lines.append("      " + o.short())
    for i, s in enumerate(result.steps, 1):
        lines.append(f"  {i:2}. {s.phase:12} {s.operation:26} {s.system_id:20} {s.resource_id:18} {s.state:10} {s.evidence_id}")
    for n in result.notes:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
