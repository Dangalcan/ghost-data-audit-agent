"""Ghost Data Still Exists - deletion audit agent.

Usage:
  python code/main.py                 # audit dataset/deletion_requests.csv -> output.csv (repo root)
  python code/main.py --sample        # audit the 32 sample requests -> code/evaluation/sample_predictions.csv
  python code/main.py --cases case_11,case_12 --explain
  python code/main.py --no-llm        # deterministic guard only, cached vision extractions only
  python code/main.py --review all --log-level DEBUG
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import log  # noqa: E402
from audit.config import OUTPUT_PATH, REPO_ROOT  # noqa: E402
from audit.loaders import CaseRequest, load_requests  # noqa: E402
from audit.planner import CaseResult, audit_case, explain  # noqa: E402

logger = log.get("main")
SAMPLE_OUT = REPO_ROOT / "code" / "evaluation" / "sample_predictions.csv"


def _case_number(case_id: str) -> tuple[int, str]:
    tail = case_id.rsplit("_", 1)[-1]
    return (int(tail) if tail.isdigit() else 0, case_id)


def write_output(results: Iterable[CaseResult], path: Path) -> None:
    rows = sorted(results, key=lambda r: _case_number(r.case_id))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in rows:
            w.writerow([r.case_id, r.deletion_status, r.steps_json()])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="run on sample_deletion_requests.csv")
    ap.add_argument("--cases", help="comma-separated case ids to run")
    ap.add_argument("--explain", action="store_true", help="print the evidence chain for each case")
    ap.add_argument("--no-llm", action="store_true", help="never call a model (guard + cached extractions only)")
    ap.add_argument("--skip-images", action="store_true", help="ignore screenshot evidence entirely")
    ap.add_argument(
        "--out",
        help="output csv path (default: repo-root output.csv, or code/evaluation/sample_predictions.csv with --sample)",
    )
    ap.add_argument(
        "--review",
        choices=["none", "uncertain", "all"],
        default="uncertain",
        help="run the tool-using reviewer on uncertain cases (default), on all cases, or not at all",
    )
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap.parse_args(argv)


def load_env() -> None:
    """Read ANTHROPIC_API_KEY from the git-ignored .env when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def select_cases(args: argparse.Namespace) -> list[CaseRequest]:
    cases = list(load_requests(sample=args.sample))
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.case_id in wanted]
    return cases


def review_result(case: CaseRequest, result: CaseResult, args: argparse.Namespace, stats: Counter[str]) -> CaseResult:
    """Run the tool-using reviewer on an uncertain draft (or on every draft with --review all)."""
    from audit.agent import needs_review, review_case, validate_review
    from audit.planner import apply_review

    reasons = needs_review(result)
    if args.review != "all" and not reasons:
        return result
    review = validate_review(case, result, review_case(case, result))
    stats["error" if review.error else review.verdict] += 1
    stats["cached"] += int(review.cached)
    stats["accepted_vetoes"] += len(review.accepted_vetoes)
    stats["accepted_proposals"] += len(review.accepted_proposals)
    stats["rejected"] += len(review.rejected)
    result = apply_review(case, result, review, skip_images=args.skip_images, use_llm=True)
    result.notes.append("review reasons: " + "; ".join(reasons))
    return result


def run(args: argparse.Namespace) -> tuple[list[CaseResult], Counter[str]]:
    cases = select_cases(args)
    use_review = not args.no_llm and args.review != "none"
    stats: Counter[str] = Counter()
    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        result = audit_case(case, skip_images=args.skip_images, use_llm=not args.no_llm)
        if use_review and result.deletion_status != "NOT_APPLICABLE":
            result = review_result(case, result, args, stats)
        results.append(result)
        if args.explain:
            print(explain(result))
            print()
        if i % 50 == 0 or i == len(cases):
            logger.info(log.kv(progress=f"{i}/{len(cases)}", case=case.case_id, status=result.deletion_status))
    return results, stats


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log.configure(args.log_level)
    if not args.no_llm:
        load_env()
    t0 = time.time()
    results, review_stats = run(args)
    out = Path(args.out) if args.out else (SAMPLE_OUT if args.sample else OUTPUT_PATH)
    write_output(results, out)
    mix = dict(Counter(r.deletion_status for r in results))
    print(f"{len(results)} cases -> {out} in {time.time() - t0:.1f}s; status mix: {mix}")
    if review_stats:
        print(f"reviewer: {dict(review_stats)}")
    if not args.no_llm:
        from audit.llm import usage_summary

        print(usage_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
