# Token usage and cost report

Final full-dataset run that produced `output.csv` (518 requests), plus the model calls that
produced the cached artifacts it consumed. Prices: Anthropic first-party rates (USD per 1M tokens):
Claude Haiku 5.5 $0.10 in / $0.50 out; Claude Sonnet 5.5 $2.00 in / $10.00 out (cache reads/writes priced
at the documented multipliers).

## Models

| provider | model | purpose |
|---|---|---|
| Anthropic | claude-haiku-5-5 | request_text intent classification (one call per unique text, effort=low) |
| Anthropic | claude-sonnet-5-5 | screenshot transcription to structured records (one call per PNG, effort=medium) |

## Model calls that produced the cached artifacts (real tokens)

| model | purpose | calls | input tokens | output tokens | cache read | cache write | est. cost USD |
|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | intent | 399 | 216691 | 23809 | 0 | 0 | 0.0336 |
| claude-sonnet-5-5 | vision | 11 | 29162 | 9602 | 10040 | 1004 | 0.1589 |

### Per-model totals

| model | calls | input tokens (incl. cache) | output tokens | est. cost USD |
|---|---|---|---|---|
| claude-haiku-5-5 | 399 | 216691 | 23809 | 0.0336 |
| claude-sonnet-5-5 | 11 | 40206 | 9602 | 0.1589 |

### Overall

- Model calls: **410** (399 intent + 11 vision)
- Input tokens: **256897**, output tokens: **33411**, total **290308**
- Average per request (518): **560.4 tokens**, 0.79 calls
- Estimated cost: **0.1924 USD** total, **0.000371 USD per request**

## Final full-dataset run (`python code/main.py`)

| model | purpose | calls | served from cache | new input tokens | new output tokens |
|---|---|---|---|---|---|
| claude-haiku-5-5 | intent | 4241 | 4241 | 0 | 0 |
| claude-sonnet-5-5 | vision | 11 | 11 (read from code/cache/vision) | 0 | 0 |

The final run re-used every cached response, so it added **0 new tokens / 0.0000 USD**; the
numbers above are the cost of producing those cached responses once. Request texts repeat
across cases (518 requests, 398 unique texts; 399 intent calls
including one smoke test), which is why the intent classifier is called once per unique text rather than once per case.

## Why so few tokens

- Evidence (logs, JSON, SQL, PDF text) is parsed by code, never sent to a model.
- The model only sees the request text (Haiku, ~550 tokens per call) and the 11 screenshots (Sonnet).
- Disk cache keyed by model + prompt + content; the usage ledger records every call (cached or not).
- Deterministic engine: reruns are free, reproducible and auditable.
