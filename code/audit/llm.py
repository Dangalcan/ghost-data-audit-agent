"""Thin Anthropic client used for the jobs code cannot do alone.

1. classify_request_text  - multilingual intent of request_text (Claude Haiku 5.5)
2. extract_screenshot     - structured transcription of console screenshots (Claude Sonnet 5.5)
3. messages()             - shared entry point for the reviewer loop (agent.py)

Design rules
- the model describes, code decides: outputs are validated against JSON schemas
  (schemas.py) and closed vocabularies before any code consumes them;
- every call is cached on disk (code/cache/llm) keyed by model + prompt + content, so a
  re-run costs nothing and is reproducible; vision extractions are additionally stored in
  code/cache/vision/<file_id>.json where the evidence parser reads them;
- every real call is appended to code/evaluation/usage_ledger.jsonl (tokens, model,
  purpose) and usage_summary() turns that into the usage report;
- retries: the SDK retries connection errors / 429 / 5xx with exponential backoff; on top
  of that `with_retries` adds a bounded, jittered backoff for rate limits and overloads
  and never retries client errors (400/401/403/404);
- secrets come from the environment only (ANTHROPIC_API_KEY via .env, never committed);
- untrusted text (request_text, screenshot content) is wrapped as data in the user turn
  and the system prompt says so; no tool use is exposed in these two calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

from . import log
from .config import INTENT_MODEL, LLM_CACHE_DIR, PROMPTS_DIR, USAGE_LEDGER_PATH, VISION_CACHE_DIR, VISION_MODEL
from .schemas import INTENT_SCHEMA, RECORD_TYPES, VISION_SCHEMA, SchemaError, validate

logger = log.get("llm")
T = TypeVar("T")

# USD per 1M tokens (Anthropic first-party rates, Oct 2026). Used only for the cost estimate.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
MAX_ATTEMPTS = int(os.environ.get("AUDIT_LLM_MAX_ATTEMPTS", "4"))
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 30.0


class LLMUnavailable(RuntimeError):
    """No credentials or SDK: callers degrade to deterministic behaviour."""


class ModelRefused(RuntimeError):
    """The model declined the request (stop_reason == refusal)."""


# --------------------------------------------------------------------------- client + retries
@cache
def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("anthropic SDK not installed") from exc
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise LLMUnavailable("ANTHROPIC_API_KEY not set (put it in .env)")
    # Accept-Encoding pinned: the Windows `brotli` wheel installed here predates the
    # httpx2 decoder API, so Brotli-compressed responses fail to decode.
    return anthropic.Anthropic(max_retries=3, timeout=120.0, default_headers={"Accept-Encoding": "gzip, deflate"})


def _retryable(exc: Exception) -> bool:
    """Rate limits, overloads, server errors and transport errors are worth another attempt."""
    import anthropic

    if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError | anthropic.APITimeoutError):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def with_retries(fn: Callable[[], T], what: str, attempts: int = MAX_ATTEMPTS) -> T:
    """Call `fn` with bounded exponential backoff; client errors propagate immediately."""
    import anthropic

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(f"authentication failed: {exc.message}") from exc
        except Exception as exc:
            if not _retryable(exc) or attempt == attempts:
                logger.error(log.kv(call=what, attempt=attempt, error=type(exc).__name__))
                raise
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) + random.uniform(0, 1)  # noqa: S311
            logger.warning(log.kv(call=what, attempt=attempt, error=type(exc).__name__, retry_in_s=round(delay, 1)))
            time.sleep(delay)
    raise RuntimeError("unreachable")


def messages(**params: Any):
    """`client.messages.create` wrapped in typed retries; raises ModelRefused on a refusal."""
    response = with_retries(lambda: _client().messages.create(**params), what=str(params.get("model", "messages")))
    if response.stop_reason == "refusal":
        raise ModelRefused(f"model refused ({params.get('model')})")
    return response


# --------------------------------------------------------------------------- cache + ledger
def _prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ledger(entry: dict[str, Any]) -> None:
    """Append one usage row (real call or cache hit) to the usage ledger."""
    USAGE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cached_entry(model: str, purpose: str, **extra: Any) -> dict[str, Any]:
    return {
        "ts": now_iso(),
        "model": model,
        "purpose": purpose,
        "cached": True,
        "input_tokens": 0,
        "output_tokens": 0,
        **extra,
    }


def usage_entry(response: Any, model: str, purpose: str, t0: float, **extra: Any) -> dict[str, Any]:
    u = response.usage
    entry = {
        "ts": now_iso(),
        "model": model,
        "purpose": purpose,
        "cached": False,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "latency_s": round(time.time() - t0, 2),
        "request_id": getattr(response, "_request_id", None),
    }
    entry.update(extra)
    return entry


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a model's text answer (code fences tolerated)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise SchemaError("no JSON object in model output")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SchemaError(f"malformed JSON in model output: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise SchemaError("model output is not a JSON object")
    return parsed


def _call(
    model: str, system: str, content: Any, purpose: str, max_tokens: int, effort: str, cache_tag: str
) -> dict[str, Any]:
    """One Messages request with disk cache + usage ledger. Returns the parsed JSON object."""
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = LLM_CACHE_DIR / f"{purpose}_{_cache_key(model, system, cache_tag)[:24]}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        ledger(cached_entry(model, purpose))
        return cached["parsed"]
    t0 = time.time()
    response = messages(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort},
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = extract_json(text)
    entry = usage_entry(response, model, purpose, t0)
    ledger(entry)
    logger.info(
        log.kv(call=purpose, model=model, input_tokens=entry["input_tokens"], output_tokens=entry["output_tokens"])
    )
    cache_path.write_text(
        json.dumps({"usage": entry, "raw": text, "parsed": parsed}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return parsed


# --------------------------------------------------------------------------- intent
def classify_request_text(text: str) -> dict[str, str]:
    """DELETION / NON_DELETION for one untrusted request text (validated against INTENT_SCHEMA)."""
    content = f"<request_text>\n{text.strip()}\n</request_text>"
    parsed = _call(INTENT_MODEL, _prompt("request_intent.md"), content, "intent", 200, "low", cache_tag=text.strip())
    parsed["label"] = str(parsed.get("label", "")).upper()
    parsed["confidence"] = str(parsed.get("confidence", "medium")).lower()
    validate(parsed, INTENT_SCHEMA, "intent")
    return {"label": parsed["label"], "confidence": parsed["confidence"], "reason": str(parsed.get("reason", ""))[:200]}


# --------------------------------------------------------------------------- vision
def _clean_record(rec: dict[str, Any]) -> dict[str, Any]:
    rtype = str(rec.get("record_type") or "OTHER").upper()
    rec["record_type"] = rtype if rtype in RECORD_TYPES else "OTHER"
    ids = rec.get("identifiers") or {}
    rec["identifiers"] = {
        k: [str(v) for v in (ids.get(k) or [])] for k in ("emails", "user_ids", "phones", "names", "hashes")
    }
    rec["aliases"] = [str(a) for a in (rec.get("aliases") or [])]
    return rec


def extract_screenshot(path: Path, evidence_id: str) -> dict[str, Any]:
    """Transcribe one console screenshot into typed records (validated against VISION_SCHEMA)."""
    raw = path.read_bytes()
    image = {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(raw).decode("ascii")}
    content = [
        {"type": "image", "source": image},
        {"type": "text", "text": f"Screenshot file: {path.name}. Transcribe every visible record as specified."},
    ]
    tag = hashlib.sha256(raw).hexdigest()
    parsed = _call(VISION_MODEL, _prompt("vision_extract.md"), content, "vision", 4000, "medium", cache_tag=tag)
    validate(parsed, VISION_SCHEMA, f"vision {evidence_id}")
    parsed["records"] = [_clean_record(rec) for rec in parsed.get("records", []) if isinstance(rec, dict)]
    parsed["source_file"] = path.name
    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (VISION_CACHE_DIR / f"{evidence_id}.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return parsed


# --------------------------------------------------------------------------- usage
def usage_rows(ledger_path: Path = USAGE_LEDGER_PATH) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cost(model: str, d: dict[str, int]) -> float:
    p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return (
        d["input"] * p["input"]
        + d["output"] * p["output"]
        + d["cache_read"] * p["cache_read"]
        + d["cache_write"] * p["cache_write"]
    ) / 1e6


def _aggregate(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    per: dict[tuple[str, str], dict[str, int]] = {}
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


def usage_summary(ledger_path: Path = USAGE_LEDGER_PATH, requests: int = 518) -> str:
    """Markdown table: calls, tokens and estimated cost per (model, purpose) plus totals."""
    rows = usage_rows(ledger_path)
    if not rows:
        return "usage: no model calls recorded"
    lines = [
        "| model | purpose | calls (cache hits) | input tok | output tok | cache read | cache write | est. cost USD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tot_cost, tot_in, tot_out, tot_calls = 0.0, 0, 0, 0
    for (model, purpose), d in sorted(_aggregate(rows).items()):
        cost = _cost(model, d)
        tot_cost += cost
        tot_in += d["input"] + d["cache_read"] + d["cache_write"]
        tot_out += d["output"]
        tot_calls += d["calls"]
        lines.append(
            f"| {model} | {purpose} | {d['calls']} ({d['cached']}) | {d['input']} | {d['output']} | "
            f"{d['cache_read']} | {d['cache_write']} | {cost:.4f} |"
        )
    lines.append(f"| **total** | | {tot_calls} | {tot_in} | {tot_out} | | | **{tot_cost:.4f}** |")
    lines.append("")
    lines.append(
        f"Requests audited: {requests}. Total tokens: {tot_in + tot_out} "
        f"(avg {(tot_in + tot_out) / requests:.1f} per request). "
        f"Estimated cost: {tot_cost:.4f} USD total, {tot_cost / requests:.6f} USD per request."
    )
    return "\n".join(lines)
