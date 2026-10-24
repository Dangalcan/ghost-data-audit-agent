"""Thin Anthropic client used for the two jobs code cannot do alone.

1. classify_request_text  - multilingual intent of request_text (Claude Haiku 5.5)
2. extract_screenshot     - structured transcription of console screenshots (Claude Sonnet 5.5)

Design rules
- the model describes, code decides: outputs are JSON validated against a fixed vocabulary;
- every call is cached on disk (code/cache/llm) keyed by model + prompt + content, so a
  re-run costs nothing and is reproducible; vision extractions are additionally stored in
  code/cache/vision/<file_id>.json where the evidence parser reads them;
- every real call is appended to code/evaluation/usage_ledger.jsonl (tokens, model,
  purpose) and usage_summary() turns that into the usage report;
- secrets come from the environment only (ANTHROPIC_API_KEY via .env, never committed);
- untrusted text (request_text, screenshot content) is wrapped as data in the user turn
  and the system prompt says so; no tool use is exposed to the model.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from .config import INTENT_MODEL, LLM_CACHE_DIR, PROMPTS_DIR, USAGE_LEDGER_PATH, VISION_CACHE_DIR, VISION_MODEL

# USD per 1M tokens (Anthropic first-party rates, Oct 2026). Used only for the cost estimate.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
INTENT_LABELS = {"DELETION", "NON_DELETION"}
CONFIDENCES = {"high", "medium", "low"}
RECORD_TYPES = {"USER_PROFILE", "SEARCH_INDEX_ENTRY", "ANALYTICS_PROFILE", "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT",
                "USER_EXPORT", "DATA_FLOW", "BACKUP_ARCHIVE", "SECURITY_AUDIT_LOG", "ACCOUNT_TOMBSTONE",
                "MARKETING_PROFILE", "LEGAL_HOLD_RECORD", "OBJECT", "OTHER"}


class LLMUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=None)
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


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _ledger(entry: dict) -> None:
    USAGE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def _call(model: str, system: str, content, purpose: str, max_tokens: int, effort: str, cache_tag: str) -> dict:
    """Run one Messages request with disk cache + usage ledger. Returns the parsed JSON."""
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(model, system, cache_tag)
    cache_path = LLM_CACHE_DIR / f"{purpose}_{key[:24]}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        _ledger({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model, "purpose": purpose,
                 "cached": True, "input_tokens": 0, "output_tokens": 0})
        return cached["parsed"]
    client = _client()
    t0 = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model refused ({purpose})")
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = _extract_json(text)
    usage = response.usage
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model, "purpose": purpose, "cached": False,
        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "latency_s": round(time.time() - t0, 2), "request_id": getattr(response, "_request_id", None),
    }
    _ledger(entry)
    cache_path.write_text(json.dumps({"usage": entry, "raw": text, "parsed": parsed}, ensure_ascii=False, indent=1), encoding="utf-8")
    return parsed


# --------------------------------------------------------------------------- intent
def classify_request_text(text: str) -> Optional[Dict[str, str]]:
    system = _prompt("request_intent.md")
    content = f"<request_text>\n{text.strip()}\n</request_text>"
    parsed = _call(INTENT_MODEL, system, content, "intent", max_tokens=200, effort="low", cache_tag=text.strip())
    label = str(parsed.get("label", "")).upper()
    if label not in INTENT_LABELS:
        raise ValueError(f"invalid intent label {label!r}")
    conf = str(parsed.get("confidence", "medium")).lower()
    return {"label": label, "confidence": conf if conf in CONFIDENCES else "medium",
            "reason": str(parsed.get("reason", ""))[:200]}


# --------------------------------------------------------------------------- vision
def extract_screenshot(path: Path, evidence_id: str) -> dict:
    system = _prompt("vision_extract.md")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {"type": "text", "text": f"Screenshot file: {path.name}. Transcribe every visible record as specified."},
    ]
    parsed = _call(VISION_MODEL, system, content, "vision", max_tokens=4000, effort="medium",
                   cache_tag=hashlib.sha256(path.read_bytes()).hexdigest())
    records = []
    for rec in parsed.get("records", []) or []:
        if not isinstance(rec, dict):
            continue
        rtype = str(rec.get("record_type") or "OTHER").upper()
        rec["record_type"] = rtype if rtype in RECORD_TYPES else "OTHER"
        ids = rec.get("identifiers") or {}
        rec["identifiers"] = {k: [str(v) for v in (ids.get(k) or [])] for k in ("emails", "user_ids", "phones", "names", "hashes")}
        rec["aliases"] = [str(a) for a in (rec.get("aliases") or [])]
        records.append(rec)
    parsed["records"] = records
    parsed["source_file"] = path.name
    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (VISION_CACHE_DIR / f"{evidence_id}.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")
    return parsed


# --------------------------------------------------------------------------- usage
def usage_rows(ledger_path: Path = USAGE_LEDGER_PATH):
    if not ledger_path.exists():
        return []
    return [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]


def usage_summary(ledger_path: Path = USAGE_LEDGER_PATH, requests: int = 518) -> str:
    rows = usage_rows(ledger_path)
    if not rows:
        return "usage: no model calls recorded"
    per: Dict[str, dict] = {}
    for r in rows:
        key = (r["model"], r["purpose"])
        d = per.setdefault(key, {"calls": 0, "cached": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        d["calls"] += 1
        if r.get("cached"):
            d["cached"] += 1
        d["input"] += r.get("input_tokens", 0)
        d["output"] += r.get("output_tokens", 0)
        d["cache_read"] += r.get("cache_read_input_tokens", 0)
        d["cache_write"] += r.get("cache_creation_input_tokens", 0)
    lines = ["| model | purpose | calls (cache hits) | input tok | output tok | cache read | cache write | est. cost USD |",
             "|---|---|---|---|---|---|---|---|"]
    tot_cost = tot_in = tot_out = tot_calls = 0
    for (model, purpose), d in sorted(per.items()):
        p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
        cost = (d["input"] * p["input"] + d["output"] * p["output"] + d["cache_read"] * p["cache_read"]
                + d["cache_write"] * p["cache_write"]) / 1e6
        tot_cost += cost
        tot_in += d["input"] + d["cache_read"] + d["cache_write"]
        tot_out += d["output"]
        tot_calls += d["calls"]
        lines.append(f"| {model} | {purpose} | {d['calls']} ({d['cached']}) | {d['input']} | {d['output']} | "
                     f"{d['cache_read']} | {d['cache_write']} | {cost:.4f} |")
    lines.append(f"| **total** | | {tot_calls} | {tot_in} | {tot_out} | | | **{tot_cost:.4f}** |")
    lines.append("")
    lines.append(f"Requests audited: {requests}. Total tokens: {tot_in + tot_out} "
                 f"(avg {(tot_in + tot_out) / requests:.1f} per request). "
                 f"Estimated cost: {tot_cost:.4f} USD total, {tot_cost / requests:.6f} USD per request.")
    return "\n".join(lines)
