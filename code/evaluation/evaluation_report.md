# Evaluation report

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
| case-level status accuracy | **1.000** |
| row accuracy (entire ordered steps_json identical) | 0.750 |
| step precision / recall / F1 | 0.989 / 0.953 / **0.971** (tp=184, fp=2, fn=9) |
| sequence accuracy (LCS / gold length) | **0.953** |
| field accuracy - phase / system_id / resource_id / operation / state | 1.000 / 1.000 / 1.000 / 1.000 / 1.000 |
| field accuracy - evidence_id | 0.995 |

Per case (matched = steps identical in all six fields):

| case | gold status | predicted | matched / gold steps | predicted steps |
|---|---|---|---|---|
| case_01 | COMPLETE | COMPLETE | 0/1 | 1 |
| case_02 | COMPLETE | COMPLETE | 0/1 | 0 |
| case_03 | COMPLETE | COMPLETE | 2/3 | 2 |
| case_04 | COMPLETE | COMPLETE | 0/2 | 0 |
| case_05 | INCOMPLETE | INCOMPLETE | 3/3 | 3 |
| case_06 | INCOMPLETE | INCOMPLETE | 4/4 | 4 |
| case_07 | INCOMPLETE | INCOMPLETE | 4/4 | 4 |
| case_08 | INCOMPLETE | INCOMPLETE | 4/4 | 4 |
| case_09 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |
| case_10 | INCOMPLETE | INCOMPLETE | 4/4 | 4 |
| case_11 | INCOMPLETE | INCOMPLETE | 14/14 | 14 |
| case_12 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |
| case_13 | INCOMPLETE | INCOMPLETE | 8/8 | 8 |
| case_14 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |
| case_15 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |
| case_16 | INCOMPLETE | INCOMPLETE | 7/7 | 7 |
| case_17 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |
| case_18 | INCOMPLETE | INCOMPLETE | 13/14 | 13 |
| case_19 | INCOMPLETE | INCOMPLETE | 8/8 | 8 |
| case_20 | INCOMPLETE | INCOMPLETE | 7/8 | 7 |
| case_21 | COMPLETE | COMPLETE | 2/3 | 2 |
| case_22 | INCOMPLETE | INCOMPLETE | 8/8 | 8 |
| case_23 | INCOMPLETE | INCOMPLETE | 7/8 | 8 |
| case_24 | INCOMPLETE | INCOMPLETE | 7/7 | 7 |
| case_25 | COMPLETE | COMPLETE | 3/3 | 3 |
| case_26 | COMPLETE | COMPLETE | 6/6 | 6 |
| case_27 | COMPLETE | COMPLETE | 6/6 | 6 |
| case_28 | COMPLETE | COMPLETE | 6/6 | 6 |
| case_29 | INCOMPLETE | INCOMPLETE | 14/14 | 14 |
| case_30 | INCOMPLETE | INCOMPLETE | 14/14 | 14 |
| case_31 | COMPLETE | COMPLETE | 3/3 | 3 |
| case_32 | INCOMPLETE | INCOMPLETE | 5/5 | 5 |

### Remaining sample misses, by cause

| cause | steps | note |
|---|---|---|
| retained security record never mentioned in any participant file (`audit_record_6645`, `audit_record_5569`) | 2 | not derivable; we never invent ids |
| anonymised analytics profile with no identifier and an ambiguous bundle position (`analytics_9406`, `analytics_5335`, `analytics_1895`, `analytics_4566`) | 4 | attributed only when bundle order + deletion-worker run leave one consistent assignment |
| hand-built sample resources with no activity or identifier at all (`account_004`, `analytics_901`) | 2 | no link exists in the data |
| two cases sharing a deletion-worker run with identical status claims (`account_410` vs `account_7015`) | 1 | one-to-one assignment picks by case order; coin flip |
| deleted account whose only absence proof is a snapshot that never mentions the person (`account_001`, `account_002`) | 2 | step emitted with the deletion-worker event as citation instead of `evidence_54` |

## 3. Full dataset run (518 cases)

- Status mix: {'INCOMPLETE': 355, 'COMPLETE': 153, 'NOT_APPLICABLE': 10}
- Steps per case: min 0, median 6, max 14; 10 rows with `[]` (10 NOT_APPLICABLE)
- Operations emitted: VERIFY_RECORD_ABSENT 820, VERIFY_RECORD_PRESENT 425, REPORT_REMAINING_RECORD 377, DELETE_RECORD 377, REPORT_EXPOSED_PII 206, VERIFY_RECORD_REDACTED 171, VERIFY_DATA_FLOW_DISABLED 146, REDACT_RECORD 133, REPORT_ACTIVE_DATA_FLOW 113, DISABLE_DATA_FLOW 113, VERIFY_RECORD_ANONYMIZED 82, ANONYMIZE_RECORD 73, VERIFY_RESTORE_BLOCKED 59, REPORT_RESTORE_RISK 26, BLOCK_RESTORE 26
- Citations: {'evidence': 1132, 'activity': 5} (activity ids only as last resort when no file shows the record's state)
- `python code/validate_output.py output.csv`: 518 rows, 0 contract errors.
- Intent classifier vs deterministic guard: 398 unique request texts, 388 DELETION / 10 NON_DELETION, 0 disagreements;
  the 10 NON_DELETION texts coincide with the 10 `CLASSIFY_REQUEST ... non-deletion` intake rows.

## 4. Additional evaluation cases (code/evaluation/custom_cases.py)

Synthetic observations fed through the real decision core (`state_at` -> `plan_resource` ->
ordering); each case checks the exact ordered step list.

```
PASS  C1 historical email hash-only analytics contact: status=INCOMPLETE steps=5
PASS  C2 replication drift keeps the profile alive: status=INCOMPLETE steps=3
PASS  C3 non-deletion request with deletion vocabulary: status=NOT_APPLICABLE steps=0
PASS  C4 tombstone exposing identifiers: status=INCOMPLETE steps=5
PASS  C5 evidence captured after audit_at is ignored: status=INCOMPLETE steps=3
PASS  C6 complete deletion cites proof per record: status=COMPLETE steps=6
6/6 custom cases passed
```

Unit tests (`python -m pytest code/tests -q`, 11 tests): identity resolution with historical
emails and hashes, token boundaries and decoys, the six log dialects, latest-observation and
file-capture-time rules, replica drift, scoped absence (never for retained records), the policy
table, step ordering, the intent guard in four languages, and the sample regression (status
accuracy 1.0, step F1 >= 0.95).

## 4b. Agentic verification layer (code/audit/agent.py)

A Claude Sonnet 5.5 reviewer with four read-only tools tried to break the draft of every uncertain case
(93 cases: low-confidence account links in COMPLETE verdicts, unproven resources, screenshot-based
states, replica drift, medium-confidence intents). It can only change the output by citing a source that
code re-reads and confirms.

| metric | value |
|---|---|
| cases reviewed | 93 |
| verdict AGREE / DISAGREE / error | 86 / 7 / 0 |
| vetoes filed / accepted after code verification | 1 / 0 |
| proposals filed / accepted after code verification | 0 / 0 |
| mean iterations / tool calls per case | 3.8 / 7.3 |

Rejected reviewer claims, by reason:

- source carries no compliance marker for this finding: 1

Disagreements and why the output was kept:

- case_63 (high): No evidence turned up for user_09's email, phone or user id. The only linked resource is another person's record, so no remediation is needed for it....
- case_191 (low): The draft's four findings hold up, but the verdict of COMPLETE is not supported for the profile. account_6608 is UNKNOWN at audit_at. It has no evidence at all. The only trace is a deletion_worker activity (activity_2591...
- case_277 (medium): The draft's account_2352 ABSENT step rests only on activity_1847. That is a deletion-worker claim of SUCCESS. No evidence observation exists for account_2352 at or before audit_at, so its state is UNKNOWN and the COMPLET...
- case_480 (medium): The audit_record, backup, connector and support findings are supported by evidence_45. The support message reads "[REDACTED]". The connector is disabled. Restore is blocked on the backup. The audit record is retained. Th...
- case_543 (medium): The account_8931 state at audit_at is UNKNOWN. The only basis for the draft's "ABSENT" step is activity_3717, a deletion-worker claim that it submitted the delete. No evidence line shows the account absent. Searches on t...
- case_547 (medium): The draft says COMPLETE, but the primary profile account_410 has no evidence at all, so its state is UNKNOWN. The only activity for it is activity_3662, a DELETE_RECORD that the deletion worker reported as FAILED on 2026...
- case_549 (medium): The draft marks account_4842 ABSENT, but the only support is activity_3598. That is a deletion-worker claim ("submitted ... SUCCESS"), not evidence of state. No observation of account_4842 exists at or before audit_at, s...

Every disagreement concerns a COMPLETE verdict whose account-absent step rests on a deletion-worker
claim, or a catalog-linked record that shows another person's details. The contract allows no finding
without evidence of a remaining record, and the catalog row is the only ownership link for that
record, so the deterministic verdicts stand; the reviewer's objections are reported here instead.

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
| stale-finding audit: 0 of 378 findings cite a file older than the latest file mentioning the subject; 0 REDACT/ANONYMIZE records with unknown PII; 0 backups/connectors with unknown flags | sanity checks kept in the evaluation notebook (`code/evaluation/`) |

## 6. Honest assessment

The sample shows the decision core is right whenever the data links a resource to the
person; the residual error is concentrated in resources that no participant-visible
file names. On the hidden set we expect the same profile: status accuracy near 100%,
step recall limited by undiscoverable retained records and unattributable anonymised
profiles, precision kept high by never emitting ids that the data does not tie to the
subject.
