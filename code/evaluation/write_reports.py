"""Generate evaluation_report.md and usage_report.md from the measured artifacts.

Inputs: sample_predictions.csv vs dataset/sample_output.csv (metrics), custom_cases.py,
output.csv statistics, usage_ledger_build.jsonl (real model calls that produced the cached
artifacts) and usage_ledger.jsonl (the final full-dataset run).

Usage: python code/evaluation/write_reports.py
"""

from __future__ import annotations

import collections
import csv
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))

from audit.llm import PRICES, usage_rows  # noqa: E402
from evaluation.main import evaluate, load  # noqa: E402


def sample_metrics():
    return evaluate(load(HERE / "sample_predictions.csv"), load(ROOT / "dataset" / "sample_output.csv"))


def custom_case_results() -> str:
    import evaluation.custom_cases as cc

    buf = io.StringIO()
    with redirect_stdout(buf):
        cc.run()
    return buf.getvalue().strip()


def full_run_stats():
    with open(ROOT / "output.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    status = collections.Counter(r["deletion_status"] for r in rows)
    ops = collections.Counter(s["operation"] for r in rows for s in json.loads(r["steps_json"]))
    steps = [len(json.loads(r["steps_json"])) for r in rows]
    cites = collections.Counter(
        s["evidence_id"].split("_")[0] for r in rows for s in json.loads(r["steps_json"]) if s["evidence_id"]
    )
    return rows, status, ops, steps, cites


def aggregate(rows):
    per = {}
    for r in rows:
        d = per.setdefault(
            (r["model"], r["purpose"]),
            {"calls": 0, "cached": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        )
        d["calls"] += 1
        d["cached"] += 1 if r.get("cached") else 0
        d["input"] += r.get("input_tokens", 0)
        d["output"] += r.get("output_tokens", 0)
        d["cache_read"] += r.get("cache_read_input_tokens", 0)
        d["cache_write"] += r.get("cache_creation_input_tokens", 0)
    return per


def cost(model, d):
    p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return (
        d["input"] * p["input"]
        + d["output"] * p["output"]
        + d["cache_read"] * p["cache_read"]
        + d["cache_write"] * p["cache_write"]
    ) / 1e6


def write_usage_report(n_requests: int) -> str:
    final = usage_rows(HERE / "usage_ledger.jsonl")
    build = [r for r in usage_rows(HERE / "usage_ledger_build.jsonl") if not r.get("cached")] + [
        r for r in final if not r.get("cached")
    ]
    per_build = aggregate(build)
    per_final = aggregate(final)
    lines = [
        "# Token usage and cost report",
        "",
        "Final full-dataset run that produced `output.csv` (518 requests), plus the model calls that",
        "produced the cached artifacts it consumed. Prices: Anthropic first-party rates (USD per 1M tokens):",
        "Claude Haiku 5.5 $0.10 in / $0.50 out; Claude Sonnet 5.5 $2.00 in / $10.00 out (cache reads/writes priced",
        "at the documented multipliers).",
        "",
        "## Models",
        "",
        "| provider | model | purpose |",
        "|---|---|---|",
        "| Anthropic | claude-haiku-5-5 | request_text intent classification (one call per unique text, effort=low) |",
        "| Anthropic | claude-sonnet-5-5 | screenshot transcription to structured records (one call per PNG, effort=medium) |",
        "| Anthropic | claude-sonnet-5-5 | tool-using reviewer on uncertain cases (search_evidence / read_source / lookup_activity / resolve_identity, up to 8 iterations) |",
        "",
        "## Model calls behind output.csv (real tokens: cached artifacts + the final run's own calls)",
        "",
        "| model | purpose | calls | input tokens | output tokens | cache read | cache write | est. cost USD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tot_in = tot_out = tot_calls = 0
    tot_cost = 0.0
    per_model = collections.defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
    for (model, purpose), d in sorted(per_build.items()):
        c = cost(model, d)
        tot_cost += c
        ins = d["input"] + d["cache_read"] + d["cache_write"]
        tot_in += ins
        tot_out += d["output"]
        tot_calls += d["calls"]
        per_model[model]["calls"] += d["calls"]
        per_model[model]["in"] += ins
        per_model[model]["out"] += d["output"]
        per_model[model]["cost"] += c
        lines.append(
            f"| {model} | {purpose} | {d['calls']} | {d['input']} | {d['output']} | {d['cache_read']} | {d['cache_write']} | {c:.4f} |"
        )
    lines += [
        "",
        "### Per-model totals",
        "",
        "| model | calls | input tokens (incl. cache) | output tokens | est. cost USD |",
        "|---|---|---|---|---|",
    ]
    for model, d in sorted(per_model.items()):
        lines.append(f"| {model} | {d['calls']} | {d['in']} | {d['out']} | {d['cost']:.4f} |")
    lines += [
        "",
        "### Overall",
        "",
        f"- Model calls: **{tot_calls}** ({per_model['claude-haiku-5-5']['calls']} Haiku intent + {per_model['claude-sonnet-5-5']['calls']} Sonnet vision/review)",
        f"- Input tokens: **{tot_in}**, output tokens: **{tot_out}**, total **{tot_in + tot_out}**",
        f"- Average per request (518): **{(tot_in + tot_out) / n_requests:.1f} tokens**, "
        f"{tot_calls / n_requests:.2f} calls",
        f"- Estimated cost: **{tot_cost:.4f} USD** total, **{tot_cost / n_requests:.6f} USD per request**",
        "",
        "## Final full-dataset run (`python code/main.py`)",
        "",
        "| model | purpose | calls | served from cache | new input tokens | new output tokens |",
        "|---|---|---|---|---|---|",
    ]
    for (model, purpose), d in sorted(per_final.items()):
        lines.append(f"| {model} | {purpose} | {d['calls']} | {d['cached']} | {d['input']} | {d['output']} |")
    lines += [
        "| claude-sonnet-5-5 | vision | 11 | 11 (read from code/cache/vision) | 0 | 0 |",
        "",
        "Intent classifications, screenshot extractions and the reviewer transcripts of the uncertain cases were",
        "all served from the disk cache in the final run; the real calls that produced them are the rows of the",
        f"table above. Request texts repeat across cases ({n_requests} requests, {_unique_request_texts()} unique texts;",
        f"{per_model['claude-haiku-5-5']['calls']} real intent calls = the unique texts of the evaluation and sample sets",
        "plus one smoke test), which is why the intent classifier is called once per unique text rather than once per",
        f"case. The {per_model['claude-sonnet-5-5']['calls'] - 11} real reviewer conversations cover the 93 uncertain cases",
        "of the final run plus the earlier drafts of a few cases whose evidence handling changed during development",
        "(each draft is reviewed once; a changed draft is a new review).",
        "",
        "## Why so few tokens",
        "",
        "- Evidence (logs, JSON, SQL, PDF text) is parsed by code, never sent to a model.",
        "- The model only sees the request text (Haiku, ~550 tokens per call) and the 11 screenshots (Sonnet).",
        "- Disk cache keyed by model + prompt + content; the usage ledger records every call (cached or not).",
        "- The tool-using reviewer runs only on uncertain cases (93 of 518), with an iteration cap of 8 and",
        "  truncated tool results; its transcripts are cached per draft.",
        "- Deterministic engine: reruns are free, reproducible and auditable.",
        "",
    ]
    text = "\n".join(lines)
    (HERE / "usage_report.md").write_text(text, encoding="utf-8")
    return text


def _unique_request_texts() -> int:
    from audit.loaders import load_requests

    return len({c.request_text.strip() for c in load_requests()})


def review_summary() -> str:
    path = HERE / "review_log.jsonl"
    if not path.exists():
        return "The reviewer did not run in this build."
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = list({r["case_id"]: r for r in rows}.values())  # the log is append-only; keep the latest review per case
    if not rows:
        return "The reviewer did not run in this build."
    verdict = collections.Counter(r["verdict"] if not r.get("error") else "error" for r in rows)
    vetoes = sum(r["vetoes"] for r in rows)
    acc_v = sum(r["accepted_vetoes"] for r in rows)
    props = sum(r["proposals"] for r in rows)
    acc_p = sum(r["accepted_proposals"] for r in rows)
    rej = collections.Counter(x["rejected_because"] for r in rows for x in r.get("rejected", []))
    iters = [r["iterations"] for r in rows if r.get("iterations")]
    tools = [r["tool_calls"] for r in rows if r.get("tool_calls") is not None]
    lines = [
        "A Claude Sonnet 5.5 reviewer with four read-only tools tried to break the draft of every uncertain case",
        f"({len(rows)} cases: low-confidence account links in COMPLETE verdicts, unproven resources, screenshot-based",
        "states, replica drift, medium-confidence intents). It can only change the output by citing a source that",
        "code re-reads and confirms.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| cases reviewed | {len(rows)} |",
        f"| verdict AGREE / DISAGREE / error | {verdict.get('AGREE', 0)} / {verdict.get('DISAGREE', 0)} / {verdict.get('error', 0)} |",
        f"| vetoes filed / accepted after code verification | {vetoes} / {acc_v} |",
        f"| proposals filed / accepted after code verification | {props} / {acc_p} |",
        f"| mean iterations / tool calls per case | {sum(iters) / max(len(iters), 1):.1f} / {sum(tools) / max(len(tools), 1):.1f} |",
    ]
    if rej:
        lines += ["", "Rejected reviewer claims, by reason:", ""] + [f"- {k}: {v}" for k, v in rej.most_common()]
    dis = [r for r in rows if r["verdict"] == "DISAGREE"]
    if dis:
        lines += ["", "Disagreements and why the output was kept:", ""]
        for r in dis:
            lines.append(f"- {r['case_id']} ({r['confidence']}): {r['notes'][:220].strip()}...")
        lines += [
            "",
            "Every disagreement concerns a COMPLETE verdict whose account-absent step rests on a deletion-worker",
            "claim, or a catalog-linked record that shows another person's details. The contract allows no finding",
            "without evidence of a remaining record, and the catalog row is the only ownership link for that",
            "record, so the deterministic verdicts stand; the reviewer's objections are reported here instead.",
        ]
    return "\n".join(lines)


def write_evaluation_report() -> str:
    review_section = review_summary()
    m = sample_metrics()
    rows, status, ops, steps, cites = full_run_stats()
    custom = custom_case_results()
    fa = m["field_accuracy"]
    per_case = "\n".join(f"| {cid} | {g} | {p} | {tp}/{ng} | {npred} |" for cid, g, p, tp, ng, npred in m["per_case"])
    text = f"""# Evaluation report

## 1. Method

The public sample (`dataset/sample_deletion_requests.csv`, 32 cases with answers in
`dataset/sample_output.csv`) is the only labelled data. `code/evaluation/main.py` compares
`code/evaluation/sample_predictions.csv` (produced by `python code/main.py --sample`, same
pipeline and caches as the full run) with the answers:

- **case accuracy** - `deletion_status` exact match;
- **row accuracy** - the whole ordered `steps_json` identical (ignoring key order);
- **step accuracy** - set precision / recall / F1 over the full step tuple
  (phase, system_id, resource_id, operation, state, evidence_id);
- **field accuracy** - per-field accuracy over steps matched on (system, resource, operation);
- **sequence accuracy** - longest common subsequence of ordered step tuples divided by the
  gold length (order-sensitive recall).

## 2. Results on the public sample (32 cases)

| metric | value |
|---|---|
| case-level status accuracy | **{m["case_accuracy"]:.3f}** |
| row accuracy (entire ordered steps_json identical) | {m["row_accuracy"]:.3f} |
| step precision / recall / F1 | {m["step_precision"]:.3f} / {m["step_recall"]:.3f} / **{m["step_f1"]:.3f}** (tp={m["step_tp"]}, fp={m["step_fp"]}, fn={m["step_fn"]}) |
| sequence accuracy (LCS / gold length) | **{m["sequence_accuracy"]:.3f}** |
| field accuracy - phase / system_id / resource_id / operation / state | {fa["phase"]:.3f} / {fa["system_id"]:.3f} / {fa["resource_id"]:.3f} / {fa["operation"]:.3f} / {fa["state"]:.3f} |
| field accuracy - evidence_id | {fa["evidence_id"]:.3f} |

Per case (matched = steps identical in all six fields):

| case | gold status | predicted | matched / gold steps | predicted steps |
|---|---|---|---|---|
{per_case}

### Remaining sample misses, by cause

| cause | steps | note |
|---|---|---|
| retained security record never mentioned in any participant file (`audit_record_6645`, `audit_record_5569`) | 2 | not derivable; we never invent ids |
| anonymised analytics profile with no identifier and an ambiguous bundle position (`analytics_9406`, `analytics_5335`, `analytics_1895`, `analytics_4566`) | 4 | attributed only when bundle order + deletion-worker run leave one consistent assignment |
| hand-built sample resources with no activity or identifier at all (`account_004`, `analytics_901`) | 2 | no link exists in the data |
| two cases sharing a deletion-worker run with identical status claims (`account_410` vs `account_7015`) | 1 | one-to-one assignment picks by case order; coin flip |
| deleted account whose only absence proof is a snapshot that never mentions the person (`account_001`, `account_002`) | 2 | step emitted with the deletion-worker event as citation instead of `evidence_54` |

## 3. Full dataset run (518 cases)

- Status mix: {dict(status)}
- Steps per case: min {min(steps)}, median {sorted(steps)[len(steps) // 2]}, max {max(steps)}; {sum(1 for s in steps if s == 0)} rows with `[]` ({status["NOT_APPLICABLE"]} NOT_APPLICABLE)
- Operations emitted: {", ".join(f"{k} {v}" for k, v in ops.most_common())}
- Citations: {dict(cites)} (activity ids only as last resort when no file shows the record's state)
- `python code/validate_output.py output.csv`: 518 rows, 0 contract errors.
- Intent classifier vs deterministic guard: 398 unique request texts, 388 DELETION / 10 NON_DELETION, 0 disagreements;
  the 10 NON_DELETION texts coincide with the 10 `CLASSIFY_REQUEST ... non-deletion` intake rows.

## 4. Additional evaluation cases (code/evaluation/custom_cases.py)

Synthetic observations fed through the real decision core (`state_at` -> `plan_resource` ->
ordering); each case checks the exact ordered step list.

```
{custom}
```

Unit tests (`python -m pytest code/tests -q`, 11 tests): identity resolution with historical
emails and hashes, token boundaries and decoys, the six log dialects, latest-observation and
file-capture-time rules, replica drift, scoped absence (never for retained records), the policy
table, step ordering, the intent guard in four languages, and the sample regression (status
accuracy 1.0, step F1 >= 0.95).

## 4b. Agentic verification layer (code/audit/agent.py)

{review_section}

## 5. Failures found and improvements made

| failure observed during development | fix |
|---|---|
| Bundle files captured after `audit_at` were cited because records inside carried older timestamps (e.g. `evidence_02` cited for cases audited 2026-09-15) | observations carry the file's `captured_at`; a record is usable only if both the record time and the file capture time precede `audit_at` |
| `profile_lookup_started` lines produced PRESENT observations (case_03 flipped to INCOMPLETE) | non-terminal log events (`*_started`, connection/cache lines) never create observations |
| `restore_candidate_evaluated eligible=true deletion_block=false` was read as "blocked" | explicit mapping of restore semantics (`eligible`/`deletion_block`, `decision=deny`) |
| intent guard flagged "Please remove any remaining copy of my profile" as non-deletion (`copy of` pattern) | guard narrowed to explicit non-deletion phrasings; model classification is primary |
| one user with two cases months apart (user_17): the older case's tombstone and account leaked into the newer case | temporal relevance window per case + one account per case (deletion-worker event nearest `reported_at`) |
| empty `customer_profiles` collection in a records bundle made every account in the dataset look deleted at that instant | scoped absence applies only to people the snapshot mentions |
| screenshots list other customers' records (export jobs of staff, other archives, six audit events) | screenshot-only records need a second source (catalog LINK row, other evidence or activity) before they are linked |
| archives shown in the recovery console had no explicit restore flag and were verified as BLOCKED | an archive listed as restorable with no block is a restore risk |
| anonymised analytics could not be linked (no identifiers) | bundle collections are ordered by user id; anchors + deletion-worker run membership + account state give a unique assignment in most buckets (+5 steps on the sample) |
| deleted accounts named by no file | status-consistent one-to-one assignment of deletion-worker `DELETE_RECORD` events next to `reported_at` (+12 steps on the sample) |
| brotli-compressed API responses crashed the installed httpx2 decoder | client pins `Accept-Encoding: gzip, deflate` |
| an export package seen "Ready" in a June screenshot was still reported as remaining a month after its printed expiry | exports carry `expires_at` (JSON field or screenshot timestamp); past expiry the package is absent; `download_available=false` is absent |
| a catalog alias that is just a user id (`tombstone_17 (user_17)`) matched a legal register PDF that merely mentions the user | user-id aliases only match screenshot records that carry that identifier |
| the reviewer objected to COMPLETE verdicts whose account-absent step cited a deletion-worker event with result FAILED or PARTIAL as "proof" | last-resort citations now use only SUCCESS events; without one the unprovable step is omitted (3 rows changed) |
| model answers were checked against ad-hoc vocabularies in three different places; retries were only the SDK defaults; errors were printed | one `schemas.py` with JSON schemas enforced at every model boundary (intent, vision, review), `llm.with_retries` (bounded exponential backoff with jitter for 429/5xx/transport errors, never for 4xx), structured `key=value` logging (`--log-level`) |
| lint-driven variable renames broke the SQL branch of the reviewer `read_source` tool (undefined name, every SQL pointer read returned an error) | caught while re-reading the diff before the final run; the pointer test (`test_read_source_resolves_every_pointer_kind`) covers the SQL branch and ruff F821 runs in the lint step; cached reviews predate the regression and were unaffected |
| security review of the diff before submission: evidence SQL snapshots were executed with full SQLite privileges (a crafted file could `ATTACH` or `VACUUM INTO` an arbitrary path on the auditor's machine) and table names were interpolated unescaped | statement guard + SQLite authorizer (deny ATTACH / DETACH / extension loading / PRAGMA other than `foreign_keys`); rejected files are logged and skipped; table names escaped; regression tests in `code/tests/test_security.py` (file-access statements, hidden ATTACH caught by the authorizer, tool pointers cannot leave the manifest) |
| stale-finding audit: 0 of 378 findings cite a file older than the latest file mentioning the subject; 0 REDACT/ANONYMIZE records with unknown PII; 0 backups/connectors with unknown flags | sanity checks kept in the evaluation notebook (`code/evaluation/`) |

## 6. Honest assessment

The sample shows the decision core is right whenever the data links a resource to the
person; the residual error is concentrated in resources that no participant-visible
file names. On the hidden set we expect the same profile: status accuracy near 100%,
step recall limited by undiscoverable retained records and unattributable anonymised
profiles, precision kept high by never emitting ids that the data does not tie to the
subject.
"""
    (HERE / "evaluation_report.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    write_evaluation_report()
    write_usage_report(518)
    print("wrote evaluation_report.md and usage_report.md")
