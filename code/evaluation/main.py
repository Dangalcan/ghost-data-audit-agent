"""Evaluation harness: compare predictions with dataset/sample_output.csv.

Metrics
  case accuracy      deletion_status exact match
  row accuracy       whole steps_json identical as an ordered list (ignoring key order)
  step accuracy      set-based precision / recall / F1 over (phase, system, resource, operation, state, evidence)
  field accuracy     per-field accuracy over matched (system, resource, operation) triples
  sequence accuracy  LCS(pred, gold) / len(gold) over the ordered step tuples

Usage:
  python code/evaluation/main.py                       # evaluates code/evaluation/sample_predictions.csv
  python code/evaluation/main.py --pred path.csv --gold dataset/sample_output.csv --verbose
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load(path: Path):
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def tup(step: dict):
    return tuple(step.get(f, "") for f in FIELDS)


def lcs(a, b) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            dp[i + 1][j + 1] = dp[i][j] + 1 if a[i] == b[j] else max(dp[i][j + 1], dp[i + 1][j])
    return dp[len(a)][len(b)]


def evaluate(pred: dict, gold: dict, verbose: bool = False) -> dict:
    n = len(gold)
    case_ok = row_ok = 0
    tp = fp = fn = 0
    field_hits = {f: 0 for f in FIELDS}
    field_total = 0
    seq_num = seq_den = 0
    per_case = []
    for cid, g in gold.items():
        p = pred.get(cid)
        gsteps = json.loads(g["steps_json"])
        psteps = json.loads(p["steps_json"]) if p else []
        status_ok = bool(p) and p["deletion_status"] == g["deletion_status"]
        case_ok += status_ok
        gt, pt = [tup(s) for s in gsteps], [tup(s) for s in psteps]
        row_ok += status_ok and gt == pt
        gs, ps = set(gt), set(pt)
        tp += len(gs & ps)
        fp += len(ps - gs)
        fn += len(gs - ps)
        gk = {(s["system_id"], s["resource_id"], s["operation"]): s for s in gsteps}
        pk = {(s["system_id"], s["resource_id"], s["operation"]): s for s in psteps}
        for k in gk.keys() & pk.keys():
            field_total += 1
            for f in FIELDS:
                field_hits[f] += gk[k].get(f, "") == pk[k].get(f, "")
        seq_num += lcs(pt, gt)
        seq_den += max(len(gt), 1)
        per_case.append((cid, g["deletion_status"], p["deletion_status"] if p else None, len(gs & ps), len(gs), len(ps)))
        if verbose and (not status_ok or gt != pt):
            print(f"\n## {cid}: gold={g['deletion_status']} pred={p['deletion_status'] if p else None}")
            for s in gt:
                print("   gold", s, "" if s in ps else "<-- MISSING")
            for s in pt:
                if s not in gs:
                    print("   pred", s, "<-- EXTRA")
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    out = {
        "cases": n,
        "case_accuracy": case_ok / n,
        "row_accuracy": row_ok / n,
        "step_precision": prec, "step_recall": rec, "step_f1": f1,
        "step_tp": tp, "step_fp": fp, "step_fn": fn,
        "field_accuracy": {f: (field_hits[f] / field_total if field_total else 0.0) for f in FIELDS},
        "sequence_accuracy": seq_num / seq_den if seq_den else 0.0,
        "per_case": per_case,
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default=str(HERE / "sample_predictions.csv"))
    ap.add_argument("--gold", default=str(REPO / "dataset" / "sample_output.csv"))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", help="print metrics as json")
    args = ap.parse_args(argv)
    m = evaluate(load(Path(args.pred)), load(Path(args.gold)), args.verbose)
    if args.json:
        print(json.dumps({k: v for k, v in m.items() if k != "per_case"}, indent=2))
    else:
        print(f"cases={m['cases']} case_acc={m['case_accuracy']:.3f} row_acc={m['row_accuracy']:.3f} "
              f"step P/R/F1={m['step_precision']:.3f}/{m['step_recall']:.3f}/{m['step_f1']:.3f} "
              f"(tp={m['step_tp']} fp={m['step_fp']} fn={m['step_fn']}) seq_acc={m['sequence_accuracy']:.3f}")
        print("field accuracy:", {k: round(v, 3) for k, v in m["field_accuracy"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
