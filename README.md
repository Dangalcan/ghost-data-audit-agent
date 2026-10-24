# Ghost Data Still Exists - deletion audit agent

Submission for HackerRank Orchestrate (October 2026). The agent audits whether a
customer's personal data was really deleted: it resolves current and historical
identifiers, reads every evidence file (logs, JSON, SQL snapshots, PDFs, screenshots),
reconstructs each record's state at `audit_at`, applies `deletion_rules.csv` and
writes the ordered findings / remediation / verification plan for all 518 requests.

The challenge contract is in [`problem_statement.md`](./problem_statement.md) and
[`AGENTS.md`](./AGENTS.md). Design notes: [`code/docs/ARCHITECTURE.md`](./code/docs/ARCHITECTURE.md).

## Approach in one paragraph

**The model describes, deterministic code decides.** Every input format is parsed into
one stream of grounded *observations* (resource, time, presence, identifiers, flags,
evidence id). Code links observations to the subject, picks the latest observation the
auditor could have seen at `audit_at`, applies the policy and orders the steps.
Claude is used where code cannot read the input and as an independent reviewer:
Claude Haiku 5.5 classifies the multilingual `request_text` (deletion vs. non-deletion),
Claude Sonnet 5.5 transcribes the 11 console screenshots into a fixed JSON schema, and a
Sonnet 5.5 tool-using reviewer (`code/audit/agent.py`) investigates every uncertain case
and may veto a finding or propose a missed resource, but only with a citation that code
re-verifies against the raw evidence. Everything is cached on disk and logged
(`code/evaluation/usage_report.md`).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # put ANTHROPIC_API_KEY=... in .env
```

Python 3.10+ (developed on 3.12). `sqlite3` ships with Python. No other services.

## Run

```bash
python code/main.py                     # all 518 requests -> ./output.csv
python code/validate_output.py          # contract checks on ./output.csv
python code/main.py --sample            # 32 public samples -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --verbose  # metrics + per-case diff against sample_output.csv
python code/evaluation/custom_cases.py  # six hand-written cases on the decision core
python -m pytest code/tests -q          # unit tests
python code/main.py --cases case_11 --explain   # evidence chain for one case
```

Useful flags: `--review uncertain|all|none` (tool-using reviewer; default `uncertain`),
`--no-llm` (never call a model: deterministic intent guard + cached screenshot
extractions; the shipped caches make the full run reproducible offline),
`--skip-images` (ignore screenshot evidence), `--out path.csv`.

## Layout

```
code/main.py                 CLI entry point
code/audit/                  engine: config, loaders, identity, activity, evidence/*,
                             linkage, state, policy, planner, intent, llm, agent, prompts/
code/evaluation/main.py      evaluation harness (case / row / step / field / sequence)
code/evaluation/custom_cases.py   additional evaluation cases
code/evaluation/evaluation_report.md, usage_report.md   required reports
code/validate_output.py      submission validator
code/tests/                  pytest unit tests
code/cache/vision/           screenshot extractions (JSON, reviewed)
code/cache/llm/              cached model responses keyed by prompt + content
code/docs/                   ARCHITECTURE.md, INTERVIEW_NOTES.md
tools/package.py             builds code.zip;  tools/collect_agent_trace.py stages agent-trace.zip
```

## Output

`output.csv` at the repository root: `case_id,deletion_status,steps_json`, 518 rows
sorted by case id, one JSON array of steps per row following the contract (phase ->
operation table order -> system_id -> resource_id; findings cite evidence; future steps
carry an empty `evidence_id`; `NOT_APPLICABLE` rows carry `[]`).

## Security notes

Request text and screenshot content are untrusted data: they are classified or
transcribed under a system prompt that forbids following them, and model output is
validated against closed vocabularies before code uses it. The model has no tools and
never sees secrets; the API key is read from the environment only.
