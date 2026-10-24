"""Which resources belong to the subject of a case?

Sources, strongest first:
  identifier  the record itself carries one of the subject's identifiers (current or
              historical email, email hash, user_id, phone) or is trace-joined to a
              lookup keyed by one of them;
  catalog     a LINK_RESOURCE_IDENTITY row in system_activity.csv;
  alias       a catalog alias (file name / statement number) found inside a document
              or screenshot (the resource id itself never appears in that artifact);
  run         low-confidence: the account record deleted by the deletion worker next
              to reported_at when no evidence names the account (deleted accounts
              carry no identifiers any more). One-to-one across all cases.

Ownership is a static fact, so evidence captured after audit_at may be used here
(config.USE_LATE_EVIDENCE_FOR_LINKAGE). State and citations never use it.

Temporal relevance: one person can have several cases over the year (the same user
appears with different accounts in different months). A resource only counts for a
case when something (an observation or an activity row) places it inside
[requested_at - RELEVANCE_DAYS_BEFORE, audit_at].
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Set, Tuple

from . import evidence as evidence_mod
from .activity import activities_by_resource, catalog_links, parse_ts, worker_runs
from .config import USE_LATE_EVIDENCE_FOR_LINKAGE, system_for_resource
from .evidence.base import Observation, default_kind
from .identity import Subject, owners_of, resolve_subject, tokens_from_values
from .loaders import CaseRequest, load_activity, load_requests

CONFIDENCE_ORDER = {"identifier": 3, "catalog": 3, "alias": 2, "run": 1}
RELEVANCE_DAYS_BEFORE = 45
ACCOUNT_MATCH_HOURS = 3
# The workflow's claim about the case mirrors the deletion worker's claim about the
# account record (observed on every generated sample case); used only to rank candidates.
STATUS_TO_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


@dataclass
class Link:
    resource_id: str
    user_id: str
    confidence: str
    evidence_ids: Set[str] = field(default_factory=set)
    first_seen: Optional[str] = None
    note: str = ""

    @property
    def system_id(self) -> str:
        return system_for_resource(self.resource_id)


# --------------------------------------------------------------------------- aliases
@lru_cache(maxsize=None)
def alias_observations(skip_images: bool = False) -> List[Observation]:
    """Observations for catalog aliases that appear inside PDFs / screenshots."""
    out: List[Observation] = []
    links = [l for l in catalog_links() if l.alias]
    for pe in evidence_mod.all_evidence(skip_images).values():
        text = getattr(pe, "extra_text", "") or ""
        aliases = getattr(pe, "aliases", {}) or {}
        for link in links:
            if not (link.alias.lower() in text.lower() or link.alias in aliases):
                continue
            kind = default_kind(link.resource_id)
            if link.resource_id.startswith("object_"):
                kind = "object"
            out.append(Observation(
                resource_id=link.resource_id, evidence_id=pe.evidence_id, observed_at=pe.captured_at,
                presence="PRESENT", kind=kind, tokens=tokens_from_values([link.email, link.user_id]),
                pii=True, source=f"alias:{link.alias}", extra={"alias": link.alias, "catalog": link.activity_id}))
    return out


@lru_cache(maxsize=None)
def observations_with_aliases(skip_images: bool = False) -> List[Observation]:
    return evidence_mod.all_observations(skip_images) + alias_observations(skip_images)


@lru_cache(maxsize=None)
def observations_by_resource(skip_images: bool = False) -> Dict[str, List[Observation]]:
    out: Dict[str, List[Observation]] = {}
    for o in observations_with_aliases(skip_images):
        out.setdefault(o.resource_id, []).append(o)
    return out


# --------------------------------------------------------------------------- ownership
@lru_cache(maxsize=None)
def global_links(skip_images: bool = False) -> Dict[str, Dict[str, Link]]:
    """resource_id -> user_id -> Link, from every observation and catalog row."""
    links: Dict[str, Dict[str, Link]] = {}

    def add(rid: str, uid: str, conf: str, ev: Optional[str], when: Optional[str], note: str = ""):
        cur = links.setdefault(rid, {}).get(uid)
        if cur is None:
            cur = Link(rid, uid, conf, set(), when, note)
            links[rid][uid] = cur
        elif CONFIDENCE_ORDER[conf] > CONFIDENCE_ORDER[cur.confidence]:
            cur.confidence, cur.note = conf, note
        if ev:
            cur.evidence_ids.add(ev)
        if when and (cur.first_seen is None or when < cur.first_seen):
            cur.first_seen = when

    for obs in observations_with_aliases(skip_images):
        for uid in owners_of(obs.tokens):
            add(obs.resource_id, uid, "identifier", obs.evidence_id, obs.observed_at, obs.source)
    for cl in catalog_links():
        if cl.user_id:
            add(cl.resource_id, cl.user_id, "catalog", cl.activity_id, cl.event_time, f"catalog {cl.activity_id}")
    return links


def owners(resource_id: str, skip_images: bool = False) -> Set[str]:
    return set(global_links(skip_images).get(resource_id, {}))


# --------------------------------------------------------------------------- relevance
def resource_times(resource_id: str, skip_images: bool = False) -> List[str]:
    times = [o.observed_at for o in observations_by_resource(skip_images).get(resource_id, [])]
    times += [a.event_time for a in activities_by_resource().get(resource_id, [])]
    return times


def relevant_for(case: CaseRequest, resource_id: str, skip_images: bool = False) -> bool:
    lo = (parse_ts(case.requested_at) - timedelta(days=RELEVANCE_DAYS_BEFORE)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return any(lo <= t <= case.audit_at for t in resource_times(resource_id, skip_images))


def _early_identifier_evidence(case: CaseRequest, subject: Subject, rid: str, skip_images: bool) -> bool:
    for o in observations_by_resource(skip_images).get(rid, []):
        if o.observed_at <= case.audit_at and subject.user_id in owners_of(o.tokens):
            return True
    return False


def owned_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> Dict[str, Link]:
    """Resources the evidence attributes to the subject and that are relevant to this case."""
    out: Dict[str, Link] = {}
    for rid, by_user in global_links(skip_images).items():
        link = by_user.get(subject.user_id)
        if link is None or not relevant_for(case, rid, skip_images):
            continue
        if not USE_LATE_EVIDENCE_FOR_LINKAGE and link.confidence != "catalog" \
                and not _early_identifier_evidence(case, subject, rid, skip_images):
            continue
        out[rid] = link
    return out


# --------------------------------------------------------------------------- account assignment
def _case_number(case_id: str) -> int:
    try:
        return int(case_id.rsplit("_", 1)[1])
    except ValueError:
        return 10 ** 9


@lru_cache(maxsize=None)
def account_assignments(skip_images: bool = False) -> Dict[str, Tuple[str, str]]:
    """case_id -> (account_id, activity_id) for cases whose account no evidence names.

    Candidates: deletion-worker DELETE_RECORD events on account_service for accounts
    nobody owns, within ACCOUNT_MATCH_HOURS of the case's reported_at and not before
    requested_at. Solved one-to-one per connected component: when counts match,
    cases ordered by case number take events ordered by time; otherwise nearest first.
    """
    cases = list(load_requests()) + list(load_requests(sample=True))
    glinks = global_links(skip_images)
    needing = []
    for c in cases:
        s = resolve_subject(c.email)
        if s is None:
            continue
        if any(r.startswith("account_") for r in owned_resources(c, s, skip_images)):
            continue
        needing.append(c)
    events = [a for a in load_activity()
              if a.source_system == "deletion_worker" and a.event_type == "DELETE_RECORD"
              and a.target_system == "account_service" and not glinks.get(a.resource_id)]
    cand: Dict[str, List] = {}
    for c in needing:
        rep, req = parse_ts(c.reported_at), parse_ts(c.requested_at)
        near = [a for a in events
                if parse_ts(a.event_time) >= req and abs((parse_ts(a.event_time) - rep).total_seconds()) <= ACCOUNT_MATCH_HOURS * 3600]
        want = STATUS_TO_RESULT.get(c.reported_status)
        matching = [a for a in near if a.reported_result == want]
        for a in (matching or near):
            cand.setdefault(c.case_id, []).append(a)
    # connected components over (case, event)
    by_event: Dict[str, List[str]] = {}
    for cid, evs in cand.items():
        for a in evs:
            by_event.setdefault(a.activity_id, []).append(cid)
    seen_c, assignments = set(), {}
    case_by_id = {c.case_id: c for c in cases}
    for cid in cand:
        if cid in seen_c:
            continue
        comp_c, comp_e, stack = set(), set(), [cid]
        while stack:
            x = stack.pop()
            if x in comp_c:
                continue
            comp_c.add(x)
            for a in cand[x]:
                if a.activity_id not in comp_e:
                    comp_e.add(a.activity_id)
                    stack.extend(by_event[a.activity_id])
        seen_c |= comp_c
        comp_events = {a.activity_id: a for x in comp_c for a in cand[x]}
        ordered_c = sorted(comp_c, key=_case_number)
        ordered_e = sorted(comp_events.values(), key=lambda a: a.event_time)
        if len(ordered_c) == len(ordered_e):
            pairs = list(zip(ordered_c, ordered_e))
        else:  # greedy nearest-first, stable on case number
            pairs, used_c, used_e = [], set(), set()
            options = sorted(((abs((parse_ts(a.event_time) - parse_ts(case_by_id[x].reported_at)).total_seconds()),
                               _case_number(x), a.event_time, x, a) for x in comp_c for a in cand[x]))
            for _, _, _, x, a in options:
                if x in used_c or a.activity_id in used_e:
                    continue
                pairs.append((x, a))
                used_c.add(x)
                used_e.add(a.activity_id)
        for x, a in pairs:
            assignments[x] = (a.resource_id, a.activity_id)
    return assignments


# --------------------------------------------------------------------------- public API
def _nearest_account(case: CaseRequest, accounts: List[str]) -> str:
    """One case concerns one account: keep the one whose deletion-worker event sits
    closest to reported_at (status-consistent events first)."""
    want = STATUS_TO_RESULT.get(case.reported_status)
    rep = parse_ts(case.reported_at)
    best, best_key = accounts[0], None
    for rid in accounts:
        for a in activities_by_resource().get(rid, []):
            if a.source_system != "deletion_worker" or a.target_system != "account_service":
                continue
            key = (0 if a.reported_result == want else 1, abs((parse_ts(a.event_time) - rep).total_seconds()))
            if best_key is None or key < best_key:
                best, best_key = rid, key
    return best


def linked_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> Dict[str, Link]:
    out = owned_resources(case, subject, skip_images)
    accounts = [r for r in out if r.startswith("account_")]
    if len(accounts) > 1:
        keep = _nearest_account(case, accounts)
        for r in accounts:
            if r != keep:
                del out[r]
    assigned = account_assignments(skip_images).get(case.case_id)
    if assigned and not any(r.startswith("account_") for r in out):
        rid, act = assigned
        out[rid] = Link(rid, subject.user_id, "run", {act}, None, f"deletion-worker event {act} next to reported_at")
    return out


_linked_resources_base = linked_resources


# --------------------------------------------------------------------------- anonymised analytics
# Records-assurance snapshot bundles list the records of one collection ordered by the
# owner's user id. Anonymised analytics profiles carry no identifier, but their position
# between identifiable neighbours bounds the owner's user id; the deletion-worker run that
# anonymised them bounds the owner further (only users whose account was processed in the
# same run). When those constraints leave exactly one consistent assignment, we use it.
def _uid_num(uid: str) -> int:
    return int(uid.rsplit("_", 1)[1])


@lru_cache(maxsize=None)
def _case_accounts(skip_images: bool = False) -> Dict[str, Tuple[str, str]]:
    """case_id -> (user_id, account_id) for every case with a linked account."""
    out = {}
    for c in list(load_requests()) + list(load_requests(sample=True)):
        s = resolve_subject(c.email)
        if s is None:
            continue
        acc = [r for r in _linked_resources_base(c, s, skip_images) if r.startswith("account_")]
        if acc:
            out[c.case_id] = (s.user_id, acc[0])
    return out


@lru_cache(maxsize=None)
def _run_users(skip_images: bool = False) -> List[Tuple[object, Dict[str, str]]]:
    """[(run, {user_id: case_id})] from the account deletion events inside each run."""
    acc_to_case = {acc: (cid, uid) for cid, (uid, acc) in _case_accounts(skip_images).items()}
    out = []
    for run in worker_runs():
        users: Dict[str, str] = {}
        for ev in run.events:
            if ev.event_type == "DELETE_RECORD" and ev.resource_id in acc_to_case:
                cid, uid = acc_to_case[ev.resource_id]
                users[uid] = cid
        out.append((run, users))
    return out


def _run_for_activity(activity_id: str):
    for run, users in _run_users():
        if any(e.activity_id == activity_id for e in run.events):
            return run, users
    return None, {}


@lru_cache(maxsize=None)
def orphan_analytics_assignments(skip_images: bool = False) -> Dict[str, Link]:
    """case_id -> Link for anonymised analytics records attributed by bundle order."""
    from .state import state_at
    from . import evidence as evmod
    glinks = global_links(skip_images)
    obs_by_rid = observations_by_resource(skip_images)
    users_with_analytics = {uid for rid, by_user in glinks.items() if rid.startswith("analytics_") for uid in by_user}
    acc_by_case = _case_accounts(skip_images)
    assignments: Dict[str, Link] = {}
    for pe in evmod.all_evidence(skip_images).values():
        seq = [o for o in pe.observations if o.resource_id.startswith("analytics_") and "snapshot_bundle" in o.source]
        # group by snapshot index (source like name:snapshots[i][j])
        groups: Dict[str, List[Observation]] = {}
        for o in seq:
            groups.setdefault(o.source.split("]")[0], []).append(o)
        for _, items in groups.items():
            owners = [sorted(glinks.get(o.resource_id, {})) for o in items]
            if all(owners):
                continue
            snap_time = items[0].observed_at
            cands: List[List[str]] = []
            for k, o in enumerate(items):
                if owners[k]:
                    cands.append([owners[k][0]])
                    continue
                prev = max([_uid_num(owners[j][0]) for j in range(k) if owners[j]] or [0])
                nxt = min([_uid_num(owners[j][0]) for j in range(k + 1, len(items)) if owners[j]] or [10 ** 9])
                acts = [a for a in activities_by_resource().get(o.resource_id, []) if a.event_type == "ANONYMIZE_RECORD"]
                run_users: Dict[str, str] = {}
                for a in acts:
                    _, ru = _run_for_activity(a.activity_id)
                    run_users.update(ru)
                pool = []
                for uid, cid in run_users.items():
                    n = _uid_num(uid)
                    if not (prev < n < nxt) or uid in users_with_analytics:
                        continue
                    acc = acc_by_case.get(cid, (None, None))[1]
                    st = state_at(acc, snap_time, obs_by_rid.get(acc, []), evmod.scopes_for(uid, skip_images)) if acc else None
                    if st is not None and st.presence == "PRESENT":
                        continue
                    pool.append(uid)
                cands.append(sorted(set(pool), key=_uid_num))
            # enumerate strictly increasing assignments
            solutions: List[List[str]] = []

            def rec(k: int, prev_n: int, acc: List[str]):
                if len(solutions) > 50:
                    return
                if k == len(cands):
                    solutions.append(list(acc))
                    return
                for uid in cands[k]:
                    if _uid_num(uid) > prev_n:
                        rec(k + 1, _uid_num(uid), acc + [uid])
            rec(0, 0, [])
            if not solutions:
                continue
            for k, o in enumerate(items):
                if owners[k]:
                    continue
                choices = {sol[k] for sol in solutions}
                if len(choices) != 1:
                    continue
                uid = choices.pop()
                cid = next((c for c, (u, _) in acc_by_case.items() if u == uid), None)
                if cid is None or cid in assignments:
                    continue
                assignments[cid] = Link(o.resource_id, uid, "run", {pe.evidence_id}, o.observed_at,
                                        f"anonymised analytics attributed by bundle order in {pe.evidence_id}")
    return assignments


def linked_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> Dict[str, Link]:  # noqa: F811
    out = _linked_resources_base(case, subject, skip_images)
    if not any(r.startswith("analytics_") for r in out):
        extra = orphan_analytics_assignments(skip_images).get(case.case_id)
        if extra and relevant_for(case, extra.resource_id, skip_images):
            out[extra.resource_id] = extra
    return out
