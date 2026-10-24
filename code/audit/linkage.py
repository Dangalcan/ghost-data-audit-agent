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

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache

from . import evidence as evidence_mod
from .activity import WorkerRun, activities_by_resource, catalog_links, parse_ts, worker_runs
from .config import USE_LATE_EVIDENCE_FOR_LINKAGE, system_for_resource
from .evidence.base import Observation, default_kind
from .identity import Subject, owners_of, resolve_subject, tokens_from_values
from .loaders import Activity, CaseRequest, load_activity, load_evidence_manifest, load_requests

CONFIDENCE_ORDER = {"identifier": 3, "catalog": 3, "alias": 2, "run": 1}
RELEVANCE_DAYS_BEFORE = 45
ACCOUNT_MATCH_HOURS = 3
# The workflow's claim about the case mirrors the deletion worker's claim about the
# account record (observed on every generated sample case); used only to rank candidates.
STATUS_TO_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
ALWAYS_PII_KINDS = ("object", "export", "profile", "search_entry", "marketing_profile", "support_attachment")
MAX_SOLUTIONS = 50


@dataclass
class Link:
    resource_id: str
    user_id: str
    confidence: str
    evidence_ids: set[str] = field(default_factory=set)
    first_seen: str | None = None
    note: str = ""

    @property
    def system_id(self) -> str:
        return system_for_resource(self.resource_id)


def _all_cases() -> list[CaseRequest]:
    return list(load_requests()) + list(load_requests(sample=True))


# --------------------------------------------------------------------------- aliases
def _record_names(rec: dict) -> list[str]:
    ids = rec.get("identifiers") or {}
    names = [a.lower() for a in (rec.get("aliases") or [])] + [str(rec.get("resource_id") or "").lower()]
    return names + [v.lower() for k in ("user_ids", "emails") for v in (ids.get(k) or [])]


def _alias_match(link, pe) -> tuple[bool, dict | None]:
    """Does the catalog alias occur in this evidence file? Returns (hit, screenshot record)."""
    records = getattr(pe, "records", []) or []
    matched = next((rec for rec in records if link.alias.lower() in _record_names(rec)), None)
    if matched is not None:
        return True, matched
    if re.fullmatch(r"user_\d+", link.alias.lower()):  # a bare user id only counts inside a screenshot record
        return False, None
    text = (getattr(pe, "extra_text", "") or "").lower()
    aliases = getattr(pe, "aliases", {}) or {}
    return link.alias.lower() in text or link.alias in aliases, None


@cache
def alias_observations(skip_images: bool = False) -> list[Observation]:
    """Observations for catalog aliases that appear inside PDFs / screenshots."""
    out: list[Observation] = []
    links = [link for link in catalog_links() if link.alias]
    for pe in evidence_mod.all_evidence(skip_images).values():
        for link in links:
            hit, matched = _alias_match(link, pe)
            if not hit:
                continue
            kind = "object" if link.resource_id.startswith("object_") else default_kind(link.resource_id)
            pii, status = True, "PRESENT"
            if matched is not None:
                ids = matched.get("identifiers") or {}
                pii = bool(ids.get("emails") or ids.get("phones") or ids.get("names") or ids.get("hashes"))
                if str(matched.get("status") or "").upper() in ("ABSENT", "NOT_FOUND", "DELETED"):
                    status = "ABSENT"
            if kind in ALWAYS_PII_KINDS:
                pii = True
            out.append(
                Observation(
                    resource_id=link.resource_id,
                    evidence_id=pe.evidence_id,
                    observed_at=pe.captured_at,
                    presence=status,
                    kind=kind,
                    tokens=tokens_from_values([link.email, link.user_id]),
                    pii=pii,
                    source=f"alias:{link.alias}",
                    extra={"alias": link.alias, "catalog": link.activity_id},
                )
            )
    return out


@cache
def observations_with_aliases(skip_images: bool = False) -> list[Observation]:
    return evidence_mod.all_observations(skip_images) + alias_observations(skip_images)


@cache
def observations_by_resource(skip_images: bool = False) -> dict[str, list[Observation]]:
    out: dict[str, list[Observation]] = {}
    for o in observations_with_aliases(skip_images):
        out.setdefault(o.resource_id, []).append(o)
    return out


# --------------------------------------------------------------------------- ownership
class _LinkIndex:
    """resource_id -> user_id -> Link, keeping the strongest confidence per pair."""

    def __init__(self) -> None:
        self.links: dict[str, dict[str, Link]] = {}

    def add(self, rid: str, uid: str, conf: str, ev: str | None, when: str | None, note: str = "") -> None:
        cur = self.links.setdefault(rid, {}).get(uid)
        if cur is None:
            cur = Link(rid, uid, conf, set(), when, note)
            self.links[rid][uid] = cur
        elif CONFIDENCE_ORDER[conf] > CONFIDENCE_ORDER[cur.confidence]:
            cur.confidence, cur.note = conf, note
        if ev:
            cur.evidence_ids.add(ev)
        if when and (cur.first_seen is None or when < cur.first_seen):
            cur.first_seen = when


@cache
def global_links(skip_images: bool = False) -> dict[str, dict[str, Link]]:
    """resource_id -> user_id -> Link, from every observation and catalog row."""
    index = _LinkIndex()
    image_files = {e.file_id for e in load_evidence_manifest() if e.ext == "png"}
    observations = observations_with_aliases(skip_images)
    corroborated = {o.resource_id for o in observations if o.evidence_id not in image_files}
    corroborated |= set(activities_by_resource()) | {cl.resource_id for cl in catalog_links()}
    for obs in observations:
        if obs.evidence_id in image_files and obs.resource_id not in corroborated:
            continue  # a record seen only in one screenshot: no second source ties it to anyone
        for uid in owners_of(obs.tokens):
            index.add(obs.resource_id, uid, "identifier", obs.evidence_id, obs.observed_at, obs.source)
    for cl in catalog_links():
        if cl.user_id:
            index.add(cl.resource_id, cl.user_id, "catalog", cl.activity_id, cl.event_time, f"catalog {cl.activity_id}")
    return index.links


def owners(resource_id: str, skip_images: bool = False) -> set[str]:
    return set(global_links(skip_images).get(resource_id, {}))


# --------------------------------------------------------------------------- relevance
def resource_times(resource_id: str, skip_images: bool = False) -> list[str]:
    times = [o.observed_at for o in observations_by_resource(skip_images).get(resource_id, [])]
    times += [a.event_time for a in activities_by_resource().get(resource_id, [])]
    return times


def relevant_for(case: CaseRequest, resource_id: str, skip_images: bool = False) -> bool:
    lo = (parse_ts(case.requested_at) - timedelta(days=RELEVANCE_DAYS_BEFORE)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return any(lo <= t <= case.audit_at for t in resource_times(resource_id, skip_images))


def _early_identifier_evidence(case: CaseRequest, subject: Subject, rid: str, skip_images: bool) -> bool:
    return any(
        o.observed_at <= case.audit_at and subject.user_id in owners_of(o.tokens)
        for o in observations_by_resource(skip_images).get(rid, [])
    )


def owned_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> dict[str, Link]:
    """Resources the evidence attributes to the subject and that are relevant to this case."""
    out: dict[str, Link] = {}
    for rid, by_user in global_links(skip_images).items():
        link = by_user.get(subject.user_id)
        if link is None or not relevant_for(case, rid, skip_images):
            continue
        if (
            not USE_LATE_EVIDENCE_FOR_LINKAGE
            and link.confidence != "catalog"
            and not _early_identifier_evidence(case, subject, rid, skip_images)
        ):
            continue
        out[rid] = link
    return out


# --------------------------------------------------------------------------- account assignment
def _case_number(case_id: str) -> int:
    try:
        return int(case_id.rsplit("_", 1)[1])
    except ValueError:
        return 10**9


def _cases_without_account(skip_images: bool) -> list[CaseRequest]:
    needing = []
    for c in _all_cases():
        s = resolve_subject(c.email)
        if s is not None and not any(r.startswith("account_") for r in owned_resources(c, s, skip_images)):
            needing.append(c)
    return needing


def _unowned_account_deletions(skip_images: bool) -> list[Activity]:
    glinks = global_links(skip_images)
    return [
        a
        for a in load_activity()
        if a.source_system == "deletion_worker"
        and a.event_type == "DELETE_RECORD"
        and a.target_system == "account_service"
        and not glinks.get(a.resource_id)
    ]


def _candidate_events(case: CaseRequest, events: Iterable[Activity]) -> list[Activity]:
    """Deletion events near reported_at; status-consistent ones first, any otherwise."""
    rep, req = parse_ts(case.reported_at), parse_ts(case.requested_at)
    near = [
        a
        for a in events
        if parse_ts(a.event_time) >= req
        and abs((parse_ts(a.event_time) - rep).total_seconds()) <= ACCOUNT_MATCH_HOURS * 3600
    ]
    want = STATUS_TO_RESULT.get(case.reported_status)
    return [a for a in near if a.reported_result == want] or near


def _components(cand: dict[str, list[Activity]]) -> list[set[str]]:
    """Connected components of cases sharing candidate events."""
    by_event: dict[str, list[str]] = {}
    for cid, evs in cand.items():
        for a in evs:
            by_event.setdefault(a.activity_id, []).append(cid)
    seen: set[str] = set()
    comps: list[set[str]] = []
    for cid in cand:
        if cid in seen:
            continue
        comp, stack = set(), [cid]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            for a in cand[x]:
                stack.extend(by_event[a.activity_id])
        seen |= comp
        comps.append(comp)
    return comps


def _pair_component(comp: set[str], cand: dict[str, list[Activity]], case_by_id: dict[str, CaseRequest]):
    """One-to-one pairing: equal counts -> case order vs time order; else nearest first."""
    comp_events = {a.activity_id: a for x in comp for a in cand[x]}
    ordered_c = sorted(comp, key=_case_number)
    ordered_e = sorted(comp_events.values(), key=lambda a: a.event_time)
    if len(ordered_c) == len(ordered_e):
        return list(zip(ordered_c, ordered_e, strict=True))
    pairs: list[tuple[str, Activity]] = []
    used_c: set[str] = set()
    used_e: set[str] = set()
    options = sorted(
        (
            abs((parse_ts(a.event_time) - parse_ts(case_by_id[x].reported_at)).total_seconds()),
            _case_number(x),
            a.event_time,
            x,
            a,
        )
        for x in comp
        for a in cand[x]
    )
    for _, _, _, x, a in options:
        if x in used_c or a.activity_id in used_e:
            continue
        pairs.append((x, a))
        used_c.add(x)
        used_e.add(a.activity_id)
    return pairs


@cache
def account_assignments(skip_images: bool = False) -> dict[str, tuple[str, str]]:
    """case_id -> (account_id, activity_id) for cases whose account no evidence names.

    Candidates: deletion-worker DELETE_RECORD events on account_service for accounts
    nobody owns, within ACCOUNT_MATCH_HOURS of the case's reported_at and not before
    requested_at. Solved one-to-one per connected component: when counts match,
    cases ordered by case number take events ordered by time; otherwise nearest first.
    """
    events = _unowned_account_deletions(skip_images)
    cand = {c.case_id: evs for c in _cases_without_account(skip_images) if (evs := _candidate_events(c, events))}
    case_by_id = {c.case_id: c for c in _all_cases()}
    assignments: dict[str, tuple[str, str]] = {}
    for comp in _components(cand):
        for cid, a in _pair_component(comp, cand, case_by_id):
            assignments[cid] = (a.resource_id, a.activity_id)
    return assignments


# --------------------------------------------------------------------------- public API
def _nearest_account(case: CaseRequest, accounts: list[str]) -> str:
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


def _base_linked_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> dict[str, Link]:
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


def linked_resources(case: CaseRequest, subject: Subject, skip_images: bool = False) -> dict[str, Link]:
    """Every resource of the subject relevant to this case, including attributed anonymised analytics."""
    out = _base_linked_resources(case, subject, skip_images)
    if not any(r.startswith("analytics_") for r in out):
        extra = orphan_analytics_assignments(skip_images).get(case.case_id)
        if extra and relevant_for(case, extra.resource_id, skip_images):
            out[extra.resource_id] = extra
    return out


# --------------------------------------------------------------------------- anonymised analytics
# Records-assurance snapshot bundles list the records of one collection ordered by the
# owner's user id. Anonymised analytics profiles carry no identifier, but their position
# between identifiable neighbours bounds the owner's user id; the deletion-worker run that
# anonymised them bounds the owner further (only users whose account was processed in the
# same run). When those constraints leave exactly one consistent assignment, we use it.
def _uid_num(uid: str) -> int:
    return int(uid.rsplit("_", 1)[1])


@cache
def _case_accounts(skip_images: bool = False) -> dict[str, tuple[str, str]]:
    """case_id -> (user_id, account_id) for every case with a linked account."""
    out: dict[str, tuple[str, str]] = {}
    for c in _all_cases():
        s = resolve_subject(c.email)
        if s is None:
            continue
        acc = [r for r in _base_linked_resources(c, s, skip_images) if r.startswith("account_")]
        if acc:
            out[c.case_id] = (s.user_id, acc[0])
    return out


@cache
def _run_users(skip_images: bool = False) -> list[tuple[WorkerRun, dict[str, str]]]:
    """[(run, {user_id: case_id})] from the account deletion events inside each run."""
    acc_to_case = {acc: (cid, uid) for cid, (uid, acc) in _case_accounts(skip_images).items()}
    out = []
    for run in worker_runs():
        users: dict[str, str] = {}
        for ev in run.events:
            if ev.event_type == "DELETE_RECORD" and ev.resource_id in acc_to_case:
                cid, uid = acc_to_case[ev.resource_id]
                users[uid] = cid
        out.append((run, users))
    return out


def _users_in_run_of(activity_id: str) -> dict[str, str]:
    for run, users in _run_users():
        if any(e.activity_id == activity_id for e in run.events):
            return users
    return {}


class _IncreasingAssignment:
    """Enumerate assignments where user ids strictly increase along the sequence."""

    def __init__(self, candidates: list[list[str]]) -> None:
        self.candidates = candidates
        self.solutions: list[list[str]] = []

    def solve(self) -> list[list[str]]:
        self._rec(0, 0, [])
        return self.solutions

    def _rec(self, k: int, prev_n: int, acc: list[str]) -> None:
        if len(self.solutions) > MAX_SOLUTIONS:
            return
        if k == len(self.candidates):
            self.solutions.append(list(acc))
            return
        for uid in self.candidates[k]:
            if _uid_num(uid) > prev_n:
                self._rec(k + 1, _uid_num(uid), [*acc, uid])


def _candidate_users(o: Observation, prev: int, nxt: int, skip_images: bool) -> list[str]:
    """Users whose account was processed in the run that anonymised `o`, inside (prev, nxt)."""
    from .state import state_at

    glinks = global_links(skip_images)
    users_with_analytics = {uid for rid, by_user in glinks.items() if rid.startswith("analytics_") for uid in by_user}
    acc_by_case = _case_accounts(skip_images)
    obs_by_rid = observations_by_resource(skip_images)
    run_users: dict[str, str] = {}
    for a in activities_by_resource().get(o.resource_id, []):
        if a.event_type == "ANONYMIZE_RECORD":
            run_users.update(_users_in_run_of(a.activity_id))
    pool = []
    for uid, cid in run_users.items():
        if not (prev < _uid_num(uid) < nxt) or uid in users_with_analytics:
            continue
        acc = acc_by_case.get(cid, (None, None))[1]
        st = (
            state_at(acc, o.observed_at, obs_by_rid.get(acc, []), evidence_mod.scopes_for(uid, skip_images))
            if acc
            else None
        )
        if st is not None and st.presence == "PRESENT":
            continue
        pool.append(uid)
    return sorted(set(pool), key=_uid_num)


def _bundle_analytics_groups(skip_images: bool) -> Iterable[tuple[str, list[Observation]]]:
    for pe in evidence_mod.all_evidence(skip_images).values():
        groups: dict[str, list[Observation]] = {}
        for o in pe.observations:
            if o.resource_id.startswith("analytics_") and "snapshot_bundle" in o.source:
                groups.setdefault(o.source.split("]")[0], []).append(o)  # one group per snapshot
        for items in groups.values():
            yield pe.evidence_id, items


@cache
def orphan_analytics_assignments(skip_images: bool = False) -> dict[str, Link]:
    """case_id -> Link for anonymised analytics records attributed by bundle order."""
    glinks = global_links(skip_images)
    acc_by_case = _case_accounts(skip_images)
    case_of_user = {uid: cid for cid, (uid, _) in acc_by_case.items()}
    assignments: dict[str, Link] = {}
    for evidence_id, items in _bundle_analytics_groups(skip_images):
        owners_seq = [sorted(glinks.get(o.resource_id, {})) for o in items]
        if all(owners_seq):
            continue
        candidates: list[list[str]] = []
        for k, o in enumerate(items):
            if owners_seq[k]:
                candidates.append([owners_seq[k][0]])
                continue
            prev = max([_uid_num(owners_seq[j][0]) for j in range(k) if owners_seq[j]] or [0])
            nxt = min([_uid_num(owners_seq[j][0]) for j in range(k + 1, len(items)) if owners_seq[j]] or [10**9])
            candidates.append(_candidate_users(o, prev, nxt, skip_images))
        solutions = _IncreasingAssignment(candidates).solve()
        if not solutions:
            continue
        for k, o in enumerate(items):
            choices = {sol[k] for sol in solutions}
            if owners_seq[k] or len(choices) != 1:
                continue
            cid = case_of_user.get(choices.pop())
            if cid is None or cid in assignments:
                continue
            assignments[cid] = Link(
                o.resource_id,
                acc_by_case[cid][0],
                "run",
                {evidence_id},
                o.observed_at,
                f"anonymised analytics attributed by bundle order in {evidence_id}",
            )
    return assignments
