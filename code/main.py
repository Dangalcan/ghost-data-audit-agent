"""Ghost Data Still Exists - deletion audit agent.

Usage:
  python code/main.py                 # audit dataset/deletion_requests.csv -> output.csv (repo root)
  python code/main.py --sample        # audit the 32 sample requests -> code/evaluation/sample_predictions.csv
  python code/main.py --cases case_11,case_12 --explain
  python code/main.py --no-llm        # deterministic guard only, cached vision extractions only
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit.config import OUTPUT_PATH, REPO_ROOT  # noqa: E402
from audit.loaders import load_requests  # noqa: E402
from audit.planner import audit_case, explain  # noqa: E402


def write_output(results, path: Path) -> None:
    rows = sorted(results, key=lambda r: (int(r.case_id.rsplit("_", 1)[1]) if r.case_id.rsplit("_", 1)[1].isdigit() else 0, r.case_id))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in rows:
            w.writerow([r.case_id, r.deletion_status, r.steps_json()])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="run on sample_deletion_requests.csv")
    ap.add_argument("--cases", help="comma-separated case ids to run")
    ap.add_argument("--explain", action="store_true", help="print the evidence chain for each case")
    ap.add_argument("--no-llm", action="store_true", help="never call a model (guard + cached extractions only)")
    ap.add_argument("--skip-images", action="store_true", help="ignore screenshot evidence entirely")
    ap.add_argument("--out", help="output csv path (default: repo-root output.csv, or code/evaluation/sample_predictions.csv with --sample)")
    ap.add_argument("--review", choices=["none", "uncertain", "all"], default="uncertain",
                    help="run the tool-using reviewer on uncertain cases (default), on all cases, or not at all")
    args = ap.parse_args(argv)

    if not args.no_llm:
        try:
            from dotenv import load_dotenv
            load_dotenv(REPO_ROOT / ".env")
        except ImportError:
            pass
    cases = load_requests(sample=args.sample)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.case_id in wanted]
    t0 = time.time()
    results = []
    review_stats = Counter()
    for c in cases:
        r = audit_case(c, skip_images=args.skip_images, use_llm=not args.no_llm)
        if not args.no_llm and args.review != "none" and r.deletion_status != "NOT_APPLICABLE":
            from audit.agent import needs_review, review_case, validate_review
            from audit.planner import apply_review
            reasons = needs_review(r)
            if args.review == "all" or reasons:
                review = validate_review(c, r, review_case(c, r))
                review_stats[review.verdict if not review.error else "error"] += 1
                review_stats["accepted_vetoes"] += len(review.accepted_vetoes)
                review_stats["accepted_proposals"] += len(review.accepted_proposals)
                review_stats["rejected"] += len(review.rejected)
                r = apply_review(c, r, review, skip_images=args.skip_images, use_llm=not args.no_llm)
                r.notes.append("review reasons: " + "; ".join(reasons))
        results.append(r)
        if args.explain:
            print(explain(r))
            print()
    out = Path(args.out) if args.out else (REPO_ROOT / "code" / "evaluation" / "sample_predictions.csv" if args.sample else OUTPUT_PATH)
    write_output(results, out)
    print(f"{len(results)} cases -> {out} in {time.time() - t0:.1f}s; status mix: {dict(Counter(r.deletion_status for r in results))}")
    if review_stats:
        print(f"reviewer: {dict(review_stats)}")
    if not args.no_llm:
        try:
            from audit.llm import usage_summary
            print(usage_summary())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
