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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit.config import OPERATION_ORDER, OPERATION_STATE, PHASE_ORDER, phase_of  # noqa: E402
from audit.loaders import load_activity, load_audit_log, load_evidence_manifest, load_requests  # noqa: E402

STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def _num(case_id: str) -> int:
    return int(case_id.rsplit("_", 1)[1])


def validate(path: Path, sample: bool = False) -> int:
    cases = {c.case_id: c for c in load_requests(sample=sample)}
    manifest = {e.file_id: e for e in load_evidence_manifest()}
    activity = {a.activity_id: a for a in load_activity()}
    audits = {a.audit_id: a for a in load_audit_log()}
    errors = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != ["case_id", "deletion_status", "steps_json"]:
            errors.append(f"bad header {header}")
        rows = list(reader)
    ids = [r[0] for r in rows]
    if set(ids) != set(cases):
        errors.append(f"case set mismatch: missing={sorted(set(cases) - set(ids))[:5]} extra={sorted(set(ids) - set(cases))[:5]}")
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    if ids != sorted(ids, key=_num):
        errors.append("rows not sorted by case_id")
    for cid, status, sj in rows:
        c = cases.get(cid)
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{cid}: bad status {status}")
        try:
            steps = json.loads(sj)
        except json.JSONDecodeError as exc:
            errors.append(f"{cid}: invalid json ({exc})")
            continue
        if not isinstance(steps, list):
            errors.append(f"{cid}: steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE" and steps != []:
            errors.append(f"{cid}: NOT_APPLICABLE must have []")
        prev_key = None
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
            k = (PHASE_ORDER.index(s["phase"]), OPERATION_ORDER.index(op), s["system_id"], s["resource_id"])
            if prev_key is not None and k < prev_key:
                errors.append(f"{cid}: step {i} out of order")
            prev_key = k
            ev = s["evidence_id"]
            if status == "INCOMPLETE" and s["phase"] != "FINDING" and ev != "":
                errors.append(f"{cid}: step {i} future step must have empty evidence")
            if s["phase"] == "FINDING" or status == "COMPLETE":
                if not ev:
                    errors.append(f"{cid}: step {i} must cite evidence")
                elif ev in manifest:
                    if c and manifest[ev].captured_at > c.audit_at:
                        errors.append(f"{cid}: step {i} cites {ev} captured after audit_at")
                elif ev in activity:
                    if c and activity[ev].event_time > c.audit_at:
                        errors.append(f"{cid}: step {i} cites {ev} after audit_at")
                elif ev not in audits:
                    errors.append(f"{cid}: step {i} unknown evidence {ev}")
    for e in errors[:40]:
        print("ERROR", e)
    print(f"{path}: {len(rows)} rows, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    p = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "output.csv"
    raise SystemExit(validate(p, sample="--sample" in sys.argv))
