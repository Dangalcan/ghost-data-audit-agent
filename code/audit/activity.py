"""system_activity.csv helpers: deletion-worker runs, catalog links, intake hints.

Everything here is a *claim* (workflow reported results). It is used to discover
candidate resources and as a last-resort citation, never as proof of state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Dict, List, Optional, Set

from .config import LINK_WINDOW_AFTER_REPORTED_HOURS
from .identity import EMAIL_RE, USER_RE, norm_email
from .loaders import Activity, load_activity


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class CatalogLink:
    activity_id: str
    event_time: str
    resource_id: str
    target_system: str
    email: Optional[str]
    user_id: Optional[str]
    alias: Optional[str]   # e.g. "delivery_confirmation_19.pdf" or "NST-2026-0718-1842"
    exclusive: bool        # "belongs only to ... no other subject is linked"


@dataclass
class WorkerRun:
    start: datetime
    end: datetime
    events: List[Activity] = field(default_factory=list)

    @property
    def resource_ids(self) -> Set[str]:
        return {e.resource_id for e in self.events}


@lru_cache(maxsize=None)
def catalog_links() -> List[CatalogLink]:
    out: List[CatalogLink] = []
    for a in load_activity():
        if a.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        emails = EMAIL_RE.findall(a.details.lower())
        users = USER_RE.findall(a.details)
        alias = None
        m = re.search(r"\(([^)]+)\)", a.details)
        if m and m.group(1) != a.resource_id:
            alias = m.group(1)
        out.append(CatalogLink(a.activity_id, a.event_time, a.resource_id, a.target_system,
                               norm_email(emails[0]) if emails else None,
                               users[-1] if users else None, alias,
                               "only" in a.details.lower()))
    return out


@lru_cache(maxsize=None)
def non_deletion_intake_accounts() -> Dict[str, str]:
    """account_id -> activity_id for CLASSIFY_REQUEST ... non-deletion rows (a hint, not proof)."""
    return {a.resource_id: a.activity_id for a in load_activity()
            if a.event_type == "CLASSIFY_REQUEST" and "non-deletion" in a.details}


@lru_cache(maxsize=None)
def worker_runs(gap_minutes: int = 90) -> List[WorkerRun]:
    events = sorted((a for a in load_activity() if a.source_system == "deletion_worker"),
                    key=lambda a: a.event_time)
    runs: List[WorkerRun] = []
    for ev in events:
        ts = parse_ts(ev.event_time)
        if runs and (ts - runs[-1].end) <= timedelta(minutes=gap_minutes):
            runs[-1].events.append(ev)
            runs[-1].end = ts
        else:
            runs.append(WorkerRun(start=ts, end=ts, events=[ev]))
    return runs


def runs_in_window(requested_at: str, reported_at: str) -> List[WorkerRun]:
    lo = parse_ts(requested_at)
    hi = parse_ts(reported_at) + timedelta(hours=LINK_WINDOW_AFTER_REPORTED_HOURS)
    return [r for r in worker_runs() if lo <= r.start <= hi or lo <= r.end <= hi]


@lru_cache(maxsize=None)
def activities_by_resource() -> Dict[str, List[Activity]]:
    out: Dict[str, List[Activity]] = {}
    for a in load_activity():
        out.setdefault(a.resource_id, []).append(a)
    for v in out.values():
        v.sort(key=lambda a: a.event_time)
    return out


@lru_cache(maxsize=None)
def recreation_events() -> Dict[str, List[Activity]]:
    """Downstream resource -> restore/connector events that repopulated it (recreation evidence)."""
    out: Dict[str, List[Activity]] = {}
    for a in load_activity():
        m = re.search(r"repopulated downstream resource (\w+)", a.details)
        if m:
            out.setdefault(m.group(1), []).append(a)
    return out
