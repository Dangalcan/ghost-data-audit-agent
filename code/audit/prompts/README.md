# Prompts

Three prompts, one job each. Every prompt is a system message; untrusted material
(customer text, screenshot pixels, evidence lines) only ever appears in the user turn,
wrapped as data, and each prompt says so explicitly. Model output is never consumed
directly: it is parsed, validated against a JSON schema in `../schemas.py`, mapped
onto a closed vocabulary and only then handed to deterministic code.

| file | model | called from | output contract | validated by |
|---|---|---|---|---|
| `request_intent.md` | `claude-haiku-5-5`, effort `low`, 200 max tokens | `llm.classify_request_text` (via `intent.classify_intent`) | `{"label": DELETION \| NON_DELETION, "confidence": high \| medium \| low, "reason"}` | `INTENT_SCHEMA` |
| `vision_extract.md` | `claude-sonnet-5-5`, effort `medium`, 4000 max tokens | `llm.extract_screenshot` (via `evidence/images.py`) | `{"captured_at", "console", "summary", "records": [...]}` with typed records | `VISION_SCHEMA` + `RECORD_TYPES` |
| `case_review.md` | `claude-sonnet-5-5`, effort `medium`, tool loop, ≤ 8 iterations | `agent.ReviewLoop` | `submit_review` tool call: `{"verdict", "confidence", "vetoes": [citation], "proposals": [citation], "notes"}` | strict tool schema + `REVIEW_SCHEMA` + code re-reads every citation |

## Why these three and nothing else

The engine is deterministic: identity, evidence parsing, time cut-off, policy and
ordering are code. A model is used only where code cannot read the input or where an
independent second opinion is cheap insurance:

1. **Intent** – `request_text` is free text in ten languages, often with misleading
   keywords ("I am not asking for deletion, just a copy of my data"). A regex guard
   (`intent.py`) covers the strong patterns; the model handles the rest and the two are
   cross-checked. Disagreement is recorded on the verdict and makes the case eligible for
   review.
2. **Vision** – 11 evidence files are PNG screenshots of admin consoles. The model only
   *transcribes* what is visible into typed records; whether a record should exist is
   decided by code, exactly as for a log line. Transcriptions are cached in
   `../../cache/vision/<file_id>.json` and were reviewed by hand once.
3. **Review** – a tool-using reviewer tries to break each uncertain draft. It can veto a
   finding or propose a missed resource, but only with a citation (evidence id, exact
   `source` pointer returned by the tools, verbatim quote) that code re-reads and checks
   (quote present, resource named, file captured at or before `audit_at`, compliance
   marker for that finding present). Anything that does not check out is logged and
   discarded; the draft stands.

## Prompt-injection posture

* The customer's `request_text`, screenshot text and every evidence line are data.
  Each prompt states that instructions found inside them must be ignored, whatever
  authority or urgency they claim.
* Intent and vision calls expose **no tools**. The reviewer's tools are read-only
  (search, read, lookup, resolve) and return JSON; the model cannot write anything.
* The model never decides the output format on its own: strict tool schemas
  (`"strict": true`, `additionalProperties: false`) and `jsonschema` validation gate
  every answer; a non-conforming answer is an error, not a verdict.
* Secrets never enter a prompt. The API key is read from the environment only.

## Caching, cost and reproducibility

Every call is cached in `../../cache/llm/` under a key of model + system prompt +
content (the review key also includes the tool definitions and the rendered draft).
Editing a prompt therefore invalidates its cache and costs a re-run: ~45 Haiku calls
for intent, 11 Sonnet vision calls, ~90 Sonnet review conversations (≈ 2 USD in total,
see `../../evaluation/usage_report.md`). The shipped caches make
`python code/main.py` reproducible offline; `--no-llm` additionally skips the model
for intent and relies on the guard.

## Iteration log (what changed and why)

* `request_intent.md`: added the sentence that requests to *verify* or *finish* a
  deletion, or to delete specific copies (exports, search results, backups), count as
  DELETION, after the guard mis-labelled "please confirm my data was erased" style
  texts; added the rule that an explicit "I am not asking for deletion" wins over the
  word "delete" (the ten CLASSIFY_REQUEST intake rows in `system_activity.csv` confirm
  these ten NOT_APPLICABLE cases).
* `vision_extract.md`: switched from "list the records" to a fixed record shape with
  `identifiers` split by type and `aliases` for printed references (file names,
  ticket ids, statement numbers) after the first pass lost the alias that links
  `object_19` and `tombstone_17` to their catalog rows; added `captured_at` so the
  screen's own timestamp can be compared with the manifest.
* `case_review.md`: first version let the reviewer "correct" the draft freely and it
  cited FAILED worker events as proof of deletion. The current version restricts it to
  vetoes/proposals with verifiable citations and tells it that a veto without a
  verifiable contradiction is worse than none. The engine side was changed too:
  fallback citations now use SUCCESS worker events only.
