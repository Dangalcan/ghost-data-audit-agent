"""Contract validator for output.csv (run before submitting).

Checks: header, exactly one row per case in deletion_requests.csv, sorted by case_id,
valid JSON arrays, allowed phase/operation/state combos, consecutive step numbers, phase
and operation order, NOT_APPLICABLE => [], evidence ids exist and were captured at or
before audit_at, remediation / INCOMPLETE verification steps have empty evidence_id,
COMPLETE steps all cite evidence.

Usage: python code/validate_output.py [output.csv] [--sample]
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit.config import OPERATION_ORDER, OPERATION_STATE, PHASE_ORDER, phase_of  # noqa: E402
from audit.loaders import CaseRequest, load_activity, load_audit_log, load_evidence_manifest, load_requests  # noqa: E402

STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def _num(case_id: str) -> int:
    return int(case_id.rsplit("_", 1)[1])


@dataclass
class Refs:
    cases: dict[str, CaseRequest]
    evidence_captured: dict[str, str]
    activity_time: dict[str, str]
    audit_ids: set[str]


def _load_refs(sample: bool) -> Refs:
    return Refs(
        cases={c.case_id: c for c in load_requests(sample=sample)},
        evidence_captured={e.file_id: e.captured_at for e in load_evidence_manifest()},
        activity_time={a.activity_id: a.event_time for a in load_activity()},
        audit_ids={a.audit_id for a in load_audit_log()},
    )


def _check_citation(cid: str, i: int, ev: str, audit_at: str | None, refs: Refs) -> list[str]:
    if not ev:
        return [f"{cid}: step {i} must cite evidence"]
    if ev in refs.evidence_captured:
        if audit_at and refs.evidence_captured[ev] > audit_at:
            return [f"{cid}: step {i} cites {ev} captured after audit_at"]
        return []
    if ev in refs.activity_time:
        if audit_at and refs.activity_time[ev] > audit_at:
            return [f"{cid}: step {i} cites {ev} after audit_at"]
        return []
    if ev in refs.audit_ids:
        return []
    return [f"{cid}: step {i} unknown evidence {ev}"]


def _check_steps(cid: str, status: str, steps: list, audit_at: str | None, refs: Refs) -> list[str]:
    errors: list[str] = []
    prev_key: tuple | None = None
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or set(s) != STEP_KEYS:
            errors.append(f"{cid}: step {i} wrong keys")
            continue
        if s["step_no"] != i:
            errors.append(f"{cid}: step_no {s['step_no']} at position {i}")
        op = s["operation"]
        if op not in OPERATION_STATE or OPERATION_STATE[op] != s["state"] or phase_of(op) != s["phase"]:
            errors.append(f"{cid}: invalid combo {s['phase']}/{op}/{s['state']}")
            continue
        key = (PHASE_ORDER.index(s["phase"]), OPERATION_ORDER.index(op), s["system_id"], s["resource_id"])
        if prev_key is not None and key < prev_key:
            errors.append(f"{cid}: step {i} out of order")
        prev_key = key
        ev = s["evidence_id"]
        if status == "INCOMPLETE" and s["phase"] != "FINDING" and ev != "":
            errors.append(f"{cid}: step {i} future step must have empty evidence")
        if s["phase"] == "FINDING" or status == "COMPLETE":
            errors.extend(_check_citation(cid, i, ev, audit_at, refs))
    return errors


def _check_row(cid: str, status: str, steps_json: str, refs: Refs) -> list[str]:
    errors: list[str] = []
    if status not in STATUSES:
        errors.append(f"{cid}: bad status {status}")
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        return errors + [f"{cid}: invalid json ({exc})"]
    if not isinstance(steps, list):
        return errors + [f"{cid}: steps_json is not an array"]
    if status == "NOT_APPLICABLE" and steps != []:
        errors.append(f"{cid}: NOT_APPLICABLE must have []")
    case = refs.cases.get(cid)
    return errors + _check_steps(cid, status, steps, case.audit_at if case else None, refs)


def validate(path: Path, sample: bool = False) -> int:
    refs = _load_refs(sample)
    errors: list[str] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != ["case_id", "deletion_status", "steps_json"]:
            errors.append(f"bad header {header}")
        rows = list(reader)
    ids = [r[0] for r in rows]
    if set(ids) != set(refs.cases):
        missing, extra = sorted(set(refs.cases) - set(ids))[:5], sorted(set(ids) - set(refs.cases))[:5]
        errors.append(f"case set mismatch: missing={missing} extra={extra}")
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    if ids != sorted(ids, key=_num):
        errors.append("rows not sorted by case_id")
    for cid, status, steps_json in rows:
        errors.extend(_check_row(cid, status, steps_json, refs))
    for e in errors[:40]:
        print("ERROR", e)
    print(f"{path}: {len(rows)} rows, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "output.csv"
    raise SystemExit(validate(target, sample="--sample" in sys.argv))
