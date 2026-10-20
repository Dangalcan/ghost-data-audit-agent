# HackerRank Orchestrate — October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must determine whether deletion is `COMPLETE` or `INCOMPLETE`, report any remaining data or recreation risk, and produce the ordered remediation and verification steps required to finish the deletion safely.

Read [problem_statement.md](./problem_statement.md) for the complete challenge contract.

## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 500 requests to audit |
| `sample_deletion_requests.csv` | 30 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, or SQLite snapshots.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,step_no,system_id,object_id,operation,state,evidence_id
```

Each case must begin with one `ASSESS_DELETION` row, followed by its findings, remediation actions, and verification checks. Use `sample_output.csv` as the format reference.

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
evaluation/evaluation_report.md
evaluation/usage_report.md
```

`evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases, failures found, and improvements made.

`usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

## Chat Transcript

This repository includes [AGENTS.md](./AGENTS.md), which asks compatible AI coding tools to append conversation summaries to `log.txt`. Submit that log as the chat transcript. Do not include secrets in prompts, code, configuration, or logs.

## Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, README, and `evaluation/` folder |
| `output.csv` | Ordered answers for all 500 deletion requests |
| `chat_transcript` | Transcript showing how the system was developed or used |
| AI interview | Explanation of the approach and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `code.zip`.

Before submitting, confirm that:

- every case in `deletion_requests.csv` appears in `output.csv`;
- columns and step numbers follow the required format;
- findings cite valid evidence IDs;
- proposed remediation rows leave `evidence_id` empty; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
