# Ghost Data Still Exists

Build an AI-powered deletion audit agent that proves whether a user's personal data is truly gone.

A user deletes an old account. A few days later, they try to use the same email address again and the platform responds: **“This email is already registered.”**

The primary account is no longer visible, and the deletion workflow says **“Complete.”** However, the email may still exist in a secondary database, support attachment, search index, analytics export, log, archive, or backup. A synchronization or restore process may also recreate information after it was deleted.

The agent must investigate the supplied records, determine the user's current and historical identifiers, reconstruct what happened across systems, and decide whether the deletion is actually complete.

When data remains, the agent must produce the ordered remediation steps required to remove, redact, or anonymize it. When deletion has already been completed, the agent must produce the checks that prove it. The agent must also preserve records that the supplied policy requires the organization to retain.

## What You Need to Build

For each deletion request, build a system that determines:

- whether the deletion is `COMPLETE` or `INCOMPLETE`;
- which records or identifiers still expose the user;
- which active processes can recreate deleted data;
- the ordered remediation steps required to complete the request.

A deletion is complete only when:

- every relevant copy has received the treatment required by `deletion_rules.csv`;
- no active sync, import, or restore path can recreate the data;
- retained records contain only the identifiers permitted by the rules; and
- records that must be retained remain available.

## Files Provided

All participant-facing files are inside `dataset/`.

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | Requests that must be audited |
| `sample_deletion_requests.csv` | Sample requests |
| `sample_output.csv` | Correct answers for the sample requests |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Deletion, sync, export, backup, and restore activity |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of the supplied evidence files |
| `output.csv` | Blank submission template |

Evidence may be stored as PDFs, images, logs, JSON, or SQLite snapshots under `dataset/evidence/`.

## Input

Each row in `deletion_requests.csv` is one case:

```text
case_id,email,requested_at,reported_status,reported_at,audit_at,request_text
```

`reported_status` may be `COMPLETE`, `PARTIALLY_COMPLETE`, or `NOT_STARTED`. It is a claim from the existing deletion workflow, not proof of the true state.

## Required Output

Produce one or more ordered rows in `output.csv` for every case:

```text
case_id,step_no,system_id,object_id,operation,state,evidence_id
```

| Column | Meaning |
| --- | --- |
| `case_id` | Case being audited |
| `step_no` | Step order, starting at `1` |
| `system_id` | System containing the record or process |
| `object_id` | Record, export, backup, or data flow |
| `operation` | Finding, remediation action, or verification |
| `state` | Observed or required state |
| `evidence_id` | Supporting `activity_id`, `audit_id`, or `file_id` |

Every case begins with exactly one assessment row. Leave `system_id`, `object_id`, and `evidence_id` empty for that row.

```csv
case_id,step_no,system_id,object_id,operation,state,evidence_id
C0001,1,,,ASSESS_DELETION,INCOMPLETE,
C0001,2,analytics_platform,ANL000124,REPORT_REMAINING_RECORD,PRESENT,F000121
C0001,3,integration_service,FLOW000091,REPORT_ACTIVE_DATA_FLOW,ACTIVE,ACT000882
C0001,4,integration_service,FLOW000091,DISABLE_DATA_FLOW,DISABLED,
C0001,5,analytics_platform,ANL000124,DELETE_RECORD,ABSENT,
C0001,6,analytics_platform,ANL000124,VERIFY_RECORD_ABSENT,ABSENT,
C0001,7,integration_service,FLOW000091,VERIFY_DATA_FLOW_DISABLED,DISABLED,
```

## Allowed Operations

| Purpose | Operation | State |
| --- | --- | --- |
| Assessment | `ASSESS_DELETION` | `COMPLETE` or `INCOMPLETE` |
| Finding | `REPORT_REMAINING_RECORD` | `PRESENT` |
| Finding | `REPORT_EXPOSED_PII` | `PRESENT` |
| Finding | `REPORT_ACTIVE_DATA_FLOW` | `ACTIVE` |
| Finding | `REPORT_RESTORE_RISK` | `ACTIVE` |
| Remediation | `DISABLE_DATA_FLOW` | `DISABLED` |
| Remediation | `BLOCK_RESTORE` | `BLOCKED` |
| Remediation | `DELETE_RECORD` | `ABSENT` |
| Remediation | `REDACT_RECORD` | `REDACTED` |
| Remediation | `ANONYMIZE_RECORD` | `ANONYMIZED` |
| Verification | `VERIFY_RECORD_ABSENT` | `ABSENT` |
| Verification | `VERIFY_RECORD_PRESENT` | `PRESENT` |
| Verification | `VERIFY_RECORD_REDACTED` | `REDACTED` |
| Verification | `VERIFY_RECORD_ANONYMIZED` | `ANONYMIZED` |
| Verification | `VERIFY_DATA_FLOW_DISABLED` | `DISABLED` |
| Verification | `VERIFY_RESTORE_BLOCKED` | `BLOCKED` |

## Rules

- Use current and historical identifiers to find the user's data.
- Determine the state at `audit_at`. Reported statuses do not prove deletion.
- Follow `deletion_rules.csv` and identify any sync or restore process that could recreate the data.
- Order rows as: assessment, findings, remediation, verification.
- Disable data flows and block restore risks before changing records.
- Cite evidence for findings. Leave evidence empty for proposed remediation.

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring will consider the correctness of the final assessment, identity resolution, discovered records and risks, cited evidence, policy decisions, and the completeness and order of the remediation plan.

Your `code.zip` must include `evaluation/evaluation_report.md` containing:

- case-level assessment accuracy;
- row-level and field-level accuracy;
- sequence accuracy;
- at least three additional evaluation cases created by your team; and
- failures found and improvements made.

## Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, README, and `evaluation/` folder |
| `output.csv` | Ordered answers for every deletion request |
| `chat_transcript` | Transcript showing how the system was developed or used |
| AI interview | Explanation of the approach and evaluation |

### Token Usage and Cost Analysis

Your `code.zip` must include one token-usage file:

```text
evaluation/usage_report.md
```

The report must summarize the final full-dataset run that produced `output.csv`, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. If multiple models are used, include both per-model and overall totals in the same file.

Do not include API keys, credentials, or sensitive configuration values in the submission.

These are the required deliverables. Participants are encouraged to improve retrieval, multimodal interpretation, identity and deletion-state reconstruction, remediation planning, deterministic verification, batching, caching, and token efficiency.
