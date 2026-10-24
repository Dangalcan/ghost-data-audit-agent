# `code/` – the deletion-audit agent

Entry point: `python code/main.py` (from the repository root). Setup and the full
command list are in the root [`README.md`](../README.md); design rationale in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); prompt design in
[`audit/prompts/README.md`](audit/prompts/README.md).

## Pipeline per case

```
request ─► intent ─► identity ─► evidence ─► linkage ─► state@audit_at ─► policy ─► steps
             │                                                                        │
             └─ NOT_APPLICABLE, []                       uncertain? ─► reviewer (tools) ─┘
```

1. **intent** (`audit/intent.py`, `audit/llm.classify_request_text`): does the text
   authorise deletion? Model verdict validated against a schema, regex guard as
   fallback and cross-check.
2. **identity** (`audit/identity.py`): request email → stable `user_id` plus every
   identifier the person ever had (current + historical emails, sha256 hashes, phones).
3. **evidence** (`audit/evidence/*`): 94 files → one stream of `Observation`s
   (resource, time, presence, kind, identifiers, flags, evidence id). Log dialects,
   JSON bundles/exports/ledgers, SQL snapshots (sqlite3 in memory, replicas), TXT,
   PDF (pdfplumber), PNG (cached vision transcription).
4. **linkage** (`audit/linkage.py`): which resources belong to this subject *for this
   case*: identifiers → catalog rows → document aliases → deletion-worker run →
   bundle order for anonymised analytics, inside a temporal relevance window.
5. **state** (`audit/state.py`): the latest observation the auditor could have seen at
   `audit_at` (file capture time and record time both ≤ `audit_at`); any present copy
   counts; scoped snapshots imply absence only for the people they mention.
6. **policy** (`audit/policy.py`): `deletion_rules.csv` → required treatment per record
   kind → findings, remediation, verification.
7. **planner** (`audit/planner.py`): assembles, orders (phase → operation table →
   system → resource) and serialises the steps; applies validated reviewer results.
8. **reviewer** (`audit/agent.py`): tool-using Claude Sonnet 5.5 loop on uncertain
   drafts; may veto a finding or propose a resource only with a citation that code
   re-verifies. Bounded iterations, typed retries, strict schemas, append-only review
   log (`evaluation/review_log.jsonl`).

## Module map

| path | role |
|---|---|
| `main.py` | CLI: `--sample`, `--cases`, `--explain`, `--no-llm`, `--skip-images`, `--review none\|uncertain\|all`, `--out`, `--log-level` |
| `audit/config.py` | paths, flags (`USE_LATE_EVIDENCE_FOR_LINKAGE`), vocabularies (phase/operation order, resource → system) |
| `audit/loaders.py` | dataset CSVs → dataclasses (cached) |
| `audit/identity.py` | `Subject`, `Tokens`, identifier extraction with word boundaries and case folding |
| `audit/activity.py` | `system_activity.csv`: catalog links, deletion-worker runs, intake hints |
| `audit/evidence/` | `base.py` (Observation/Scope), `logs.py`, `records_json.py`, `sql.py`, `text.py`, `images.py`, registry in `__init__.py` |
| `audit/linkage.py` | ownership + temporal relevance; one-to-one account assignment; bundle-order solver |
| `audit/state.py` | state at `audit_at`, replica handling, implicit absence, export expiry |
| `audit/policy.py` | kind → category → rule → steps |
| `audit/planner.py` | per-case orchestration, ordering, `--explain` rendering, review application |
| `audit/intent.py` | guard + model intent |
| `audit/llm.py` | Anthropic client: disk cache, usage ledger, typed retries with backoff, schema validation |
| `audit/schemas.py` | JSON schemas enforced at every model boundary |
| `audit/agent.py` | reviewer tools, `ReviewLoop`, code-side validation of vetoes/proposals, `needs_review` |
| `audit/log.py` | structured `key=value` logging (`audit.*` loggers) |
| `audit/prompts/` | the three prompts + README |
| `evaluation/main.py` | metrics vs `sample_output.csv` (case / row / step / field / sequence) |
| `evaluation/custom_cases.py` | six additional evaluation cases on the decision core |
| `evaluation/write_reports.py` | generates `evaluation_report.md` and `usage_report.md` |
| `validate_output.py` | submission contract validator |
| `tests/` | pytest: parsers, identity, state, policy, ordering, reviewer tools and validation |
| `cache/` | `vision/` transcriptions, `llm/` cached model answers (shipped for offline reproducibility) |

## Guardrails checklist

* Model output → JSON schema → closed vocabulary → code. Never the other way round.
* Reviewer: iteration cap (`AUDIT_REVIEW_MAX_ITER`, default 8), read-only tools, tool
  results truncated, strict tool schemas, citations re-read by code, invalid answers
  recorded as errors and ignored.
* API: SDK retries + `llm.with_retries` (exponential backoff with jitter for 429/5xx/
  transport errors, no retry on 4xx), 120 s timeout, `stop_reason == "refusal"` handled.
* Time: nothing captured after `audit_at` decides a state or is cited
  (`validate_output.py` re-checks every citation).
* Secrets: environment only (`.env` is git-ignored, `.env.example` documents the key).
* Determinism: every model call cached; sorting rules fixed; `--no-llm` runs offline.

## Development

```bash
python -m ruff check code tools && python -m ruff format --check code tools
python -m pytest code/tests -q
python code/main.py --sample --review none && python code/evaluation/main.py
```
