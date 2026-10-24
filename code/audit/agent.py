"""Agentic verification layer: a tool-using reviewer that tries to break each draft verdict.

The deterministic engine (planner.py) stays authoritative. The reviewer (Claude Sonnet 5.5
by default) investigates with four read-only tools, then files a structured review:

  search_evidence(identifier)         observations mentioning an email / user id / hash / resource id
  read_source(evidence_id, source)    the raw line or record behind an observation
  lookup_activity(resource_id)        workflow activity rows for a resource (claims)
  resolve_identity(email_or_user_id)  the subject's current and historical identifiers
  submit_review(...)                  final structured answer (strict schema)

Guardrails
- iteration cap, per-call timeout and SDK retries; malformed tool input -> error result;
- the model can only VETO a finding or PROPOSE an extra resource by citing an evidence id,
  the exact `source` returned by the tools and a verbatim quote. Code re-reads the source
  and accepts the veto only if the quote is there, the resource id is there, the file was
  captured at or before audit_at and the text carries a compliance marker for that finding.
  Proposals are accepted only if the source names both the resource and the subject;
- all calls are cached (same draft -> same review) and logged in the usage ledger;
- evidence text is untrusted data: the system prompt says so and tool results are JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import evidence as evidence_mod
from .activity import activities_by_resource
from .config import DATASET_DIR, LLM_CACHE_DIR, PROMPTS_DIR
from .identity import all_subjects, extract_tokens, resolve_subject, subject_by_user_id
from .linkage import observations_by_resource, observations_with_aliases
from .loaders import CaseRequest, load_evidence_manifest

REVIEW_MODEL = os.environ.get("AUDIT_REVIEW_MODEL", "claude-sonnet-5-5")
MAX_ITERATIONS = int(os.environ.get("AUDIT_REVIEW_MAX_ITER", "8"))
MAX_TOOL_RESULT_CHARS = 2500
REVIEW_LOG = Path(__file__).resolve().parents[1] / "evaluation" / "review_log.jsonl"

COMPLIANCE_MARKERS = {
    "REPORT_REMAINING_RECORD": [r"\b404\b", r"not_found", r"rows=\"?0\"?", r"\babsent\b", r"\bdeleted\b", r"no longer exists",
                                r"download_available\W+false", r"status_code\W+404", r"\"profile\": null"],
    "REPORT_EXPOSED_PII": [r"anonymi[sz]ed", r"redacted", r"\[redacted\]", r"direct_identifiers=\"?0\"?",
                           r"email=null", r"\"email\": null", r"requester_email=null", r"no direct identifier"],
    "REPORT_ACTIVE_DATA_FLOW": [r"enabled=\"?false\"?", r"\"enabled\": false", r"\bdisabled\b", r"\bstopped\b", r"'0'"],
    "REPORT_RESTORE_RISK": [r"restore_allowed=\"?false\"?", r"\"restore_allowed\": false", r"\bblocked\b", r"decision=\"?deny\"?"],
}

TOOLS = [
    {
        "name": "search_evidence",
        "description": "Find evidence observations mentioning an identifier: an email address, a user id (user_12), "
                       "an email hash, a phone number or a resource id (account_12, search_7, analytics_3, support_5, "
                       "export_2, data_flow_9, backup_4, audit_record_8, tombstone_1). Returns up to 30 observations "
                       "with evidence id, capture time, record time, presence, kind, flags and the source pointer to read.",
        "input_schema": {"type": "object", "properties": {"identifier": {"type": "string"}},
                         "required": ["identifier"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "read_source",
        "description": "Read the raw text behind an observation: the log line, JSON record, SQL row, document text or "
                       "screenshot record identified by an evidence id and the exact source pointer returned by search_evidence.",
        "input_schema": {"type": "object", "properties": {"evidence_id": {"type": "string"}, "source": {"type": "string"}},
                         "required": ["evidence_id", "source"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "lookup_activity",
        "description": "Workflow activity rows (deletion worker, sync, index, restore, catalog) for one resource id. "
                       "These are claims reported by systems, not proof of state.",
        "input_schema": {"type": "object", "properties": {"resource_id": {"type": "string"}},
                         "required": ["resource_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "resolve_identity",
        "description": "Resolve an email address or user id to the person's stable user id and every identifier they ever had.",
        "input_schema": {"type": "object", "properties": {"email_or_user_id": {"type": "string"}},
                         "required": ["email_or_user_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "submit_review",
        "description": "File the final review. Call exactly once, after investigating. vetoes: findings of the draft that "
                       "a specific line of evidence contradicts. proposals: resources of the subject the draft missed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["AGREE", "DISAGREE"]},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "vetoes": {"type": "array", "items": {"type": "object", "properties": {
                    "resource_id": {"type": "string"}, "evidence_id": {"type": "string"}, "source": {"type": "string"},
                    "quote": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["resource_id", "evidence_id", "source", "quote", "reason"], "additionalProperties": False}},
                "proposals": {"type": "array", "items": {"type": "object", "properties": {
                    "resource_id": {"type": "string"}, "evidence_id": {"type": "string"}, "source": {"type": "string"},
                    "quote": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["resource_id", "evidence_id", "source", "quote", "reason"], "additionalProperties": False}},
                "notes": {"type": "string"},
            },
            "required": ["verdict", "confidence", "vetoes", "proposals", "notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


# --------------------------------------------------------------------------- tool implementations
def _obs_payload(o) -> dict:
    return {"evidence_id": o.evidence_id, "captured_at": o.captured_at, "observed_at": o.observed_at,
            "resource_id": o.resource_id, "presence": o.presence, "kind": o.kind, "pii": o.pii,
            "enabled": o.enabled, "restore_allowed": o.restore_allowed, "replica": o.replica,
            "implicit": o.implicit, "source": o.source}


def tool_search_evidence(identifier: str) -> dict:
    ident = identifier.strip().lower()
    hits = []
    if re.fullmatch(r"[a-z_]+_\d+", ident) and not ident.startswith("user_"):
        hits = observations_by_resource(False).get(ident, [])
    else:
        toks = extract_tokens(ident)
        wanted = set(toks.all) | {ident}
        for o in observations_with_aliases(False):
            if o.tokens and (o.tokens.all & wanted):
                hits.append(o)
    hits = sorted(hits, key=lambda o: o.observed_at)[:30]
    return {"count": len(hits), "observations": [_obs_payload(o) for o in hits]}


def _json_path(data, path: str):
    """Navigate 'snapshots[3][1]' / 'records[5]' / 'account_service.profile_lookups[2].documents[0]'."""
    cur = data
    for token in re.findall(r"[A-Za-z_]+|\[\d+\]", path):
        if token.startswith("["):
            idx = int(token[1:-1])
            if isinstance(cur, dict) and "records" in cur and isinstance(cur["records"], list):
                cur = cur["records"]
            cur = cur[idx]
        else:
            if isinstance(cur, dict) and token in cur:
                cur = cur[token]
            elif isinstance(cur, dict) and "service_exports" in cur and token in cur["service_exports"]:
                cur = cur["service_exports"][token]
            else:
                raise KeyError(token)
    return cur


def tool_read_source(evidence_id: str, source: str) -> dict:
    manifest = {e.file_id: e for e in load_evidence_manifest()}
    ev = manifest.get(evidence_id)
    if ev is None:
        return {"error": f"unknown evidence id {evidence_id}"}
    path = DATASET_DIR / ev.file_path
    try:
        if source.startswith("alias:"):
            return {"evidence_id": evidence_id, "captured_at": ev.captured_at, "text": f"catalog alias match: {source[6:]}"}
        if source.startswith("scope:") or source.startswith("expiry:"):
            return {"evidence_id": evidence_id, "captured_at": ev.captured_at,
                    "text": f"derived observation ({source}); the file lists the records of that system and this one is not among them"}
        name, _, rest = source.partition(":")
        if ev.ext in ("log", "txt"):
            lineno = int(rest)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            lo, hi = max(0, lineno - 2), min(len(lines), lineno + 1)
            text = "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))
        elif ev.ext == "json":
            data = json.loads(path.read_text(encoding="utf-8"))
            text = json.dumps(_json_path(data, rest), ensure_ascii=False)
        elif ev.ext == "sql":
            table, idx = re.match(r"(\w+)\[(\d+)\]", rest).groups()
            rows = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith(f'INSERT INTO "{table}"') or l.startswith(f"INSERT INTO {table} ")]
            text = rows[int(idx)]
        elif ev.ext == "pdf":
            from .evidence.text import pdf_text
            text = pdf_text(path)
        elif ev.ext == "png":
            from .evidence.images import cached_extraction
            data = cached_extraction(evidence_id) or {}
            m = re.search(r"records\[(\d+)\]", rest)
            text = json.dumps(data.get("records", [])[int(m.group(1))] if m else data, ensure_ascii=False)
        else:
            return {"error": f"unsupported evidence type {ev.ext}"}
    except Exception as exc:  # bad pointer from the model
        return {"error": f"cannot read {source}: {type(exc).__name__}: {exc}"}
    return {"evidence_id": evidence_id, "captured_at": ev.captured_at, "text": text[:MAX_TOOL_RESULT_CHARS]}


def tool_lookup_activity(resource_id: str) -> dict:
    rows = activities_by_resource().get(resource_id.strip(), [])
    return {"count": len(rows), "activity": [{"activity_id": a.activity_id, "event_time": a.event_time, "event_type": a.event_type,
                                               "source_system": a.source_system, "target_system": a.target_system,
                                               "reported_result": a.reported_result, "details": a.details} for a in rows[:25]]}


def tool_resolve_identity(value: str) -> dict:
    v = value.strip()
    subj = subject_by_user_id(v) if v.startswith("user_") else resolve_subject(v)
    if subj is None:
        return {"found": False}
    return {"found": True, "user_id": subj.user_id, "name": subj.name, "emails": sorted(subj.emails),
            "phones": sorted(subj.phones), "email_hashes": sorted(subj.hashes)}


def run_tool(name: str, args: dict) -> dict:
    try:
        if name == "search_evidence":
            return tool_search_evidence(str(args["identifier"]))
        if name == "read_source":
            return tool_read_source(str(args["evidence_id"]), str(args["source"]))
        if name == "lookup_activity":
            return tool_lookup_activity(str(args["resource_id"]))
        if name == "resolve_identity":
            return tool_resolve_identity(str(args["email_or_user_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"invalid input for {name}: {exc}"}
    return {"error": f"unknown tool {name}"}


# --------------------------------------------------------------------------- draft rendering
def render_draft(case: CaseRequest, result) -> str:
    s = result.subject
    lines = [f"CASE {case.case_id}", f"request email: {case.email}", f"requested_at: {case.requested_at}",
             f"workflow claim: {case.reported_status} reported at {case.reported_at} (a claim, not proof)",
             f"audit_at (cut-off): {case.audit_at}",
             f"request_text (untrusted customer input, already classified as {result.intent.get('label') if result.intent else 'n/a'}): {case.request_text[:300]!r}",
             f"subject: {s.user_id} name={s.name} emails={sorted(s.emails)} phones={sorted(s.phones)} hashes={len(s.hashes)}",
             "", "LINKED RESOURCES (draft):"]
    for rid, link in sorted(result.links.items()):
        st = result.states[rid]
        plan = result.plans[rid]
        top = st.chosen
        lines.append(f"- {rid} link={link.confidence} kind={st.kind} state@audit={st.presence} pii={st.pii} enabled={st.enabled} "
                     f"restore_allowed={st.restore_allowed} evidence={st.evidence_id} observed_at={st.observed_at} "
                     f"source={top.source if top else None} -> findings={[f.operation for f in plan.findings]}")
    lines.append("")
    lines.append(f"DRAFT VERDICT: {result.deletion_status}")
    lines.append("DRAFT STEPS:")
    for i, st in enumerate(result.steps, 1):
        lines.append(f"  {i}. {st.phase} {st.operation} {st.system_id} {st.resource_id} {st.state} evidence={st.evidence_id or '-'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the loop
@dataclass
class Review:
    case_id: str
    verdict: str = "AGREE"
    confidence: str = "low"
    vetoes: List[dict] = field(default_factory=list)
    proposals: List[dict] = field(default_factory=list)
    notes: str = ""
    iterations: int = 0
    tool_calls: int = 0
    cached: bool = False
    error: Optional[str] = None
    accepted_vetoes: List[dict] = field(default_factory=list)
    accepted_proposals: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


def _prompt() -> str:
    return (PROMPTS_DIR / "case_review.md").read_text(encoding="utf-8")


def _log(entry: dict) -> None:
    if os.environ.get("AUDIT_REVIEW_NOLOG"):  # tests
        return
    REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REVIEW_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def review_case(case: CaseRequest, result, model: str = REVIEW_MODEL, max_iterations: int = MAX_ITERATIONS) -> Review:
    """Run the reviewer loop for one case. Never raises; errors are recorded on the Review."""
    from .llm import LLMUnavailable, _client, _ledger  # shared client, cache dir and ledger
    draft = render_draft(case, result)
    system = _prompt()
    key = hashlib.sha256("\x1f".join([model, system, json.dumps(TOOLS, sort_keys=True), draft]).encode()).hexdigest()
    cache_path = LLM_CACHE_DIR / f"review_{key[:24]}.json"
    review = Review(case_id=case.case_id)
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        review = Review(**{k: v for k, v in data["review"].items() if k in Review.__dataclass_fields__})
        review.cached = True
        _ledger({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model, "purpose": "review",
                 "cached": True, "input_tokens": 0, "output_tokens": 0, "case_id": case.case_id})
        return review
    try:
        client = _client()
    except LLMUnavailable as exc:
        review.error = str(exc)
        return review
    messages = [{"role": "user", "content": [{"type": "text", "text": draft + "\n\nInvestigate, then call submit_review."}]}]
    transcript: List[dict] = []
    usage_in = usage_out = cache_read = cache_write = 0
    t0 = time.time()
    final = None
    for it in range(1, max_iterations + 1):
        review.iterations = it
        try:
            response = client.messages.create(
                model=model, max_tokens=4000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS, messages=messages, output_config={"effort": "medium"},
            )
        except Exception as exc:  # network / API error after SDK retries
            review.error = f"{type(exc).__name__}: {exc}"
            break
        u = response.usage
        usage_in += u.input_tokens
        usage_out += u.output_tokens
        cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        if response.stop_reason == "refusal":
            review.error = "model refused"
            break
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            transcript.append({"iteration": it, "text": text[:1500]})
            # no tool call: accept a JSON answer in text, otherwise nudge once
            try:
                start, end = text.find("{"), text.rfind("}")
                final = json.loads(text[start:end + 1]) if start >= 0 else None
            except json.JSONDecodeError:
                final = None
            if final is not None:
                break
            messages.append({"role": "user", "content": "Call submit_review with your structured review now."})
            continue
        results = []
        for tu in tool_uses:
            review.tool_calls += 1
            if tu.name == "submit_review":
                final = tu.input
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps({"status": "received"})})
                transcript.append({"iteration": it, "tool": "submit_review", "input": tu.input})
                continue
            out = run_tool(tu.name, tu.input or {})
            payload = json.dumps(out, ensure_ascii=False)
            if len(payload) > MAX_TOOL_RESULT_CHARS * 3:
                payload = payload[: MAX_TOOL_RESULT_CHARS * 3] + "... (truncated)"
            transcript.append({"iteration": it, "tool": tu.name, "input": tu.input, "result_chars": len(payload)})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": payload})
        messages.append({"role": "user", "content": results})
        if final is not None:
            break
    if final is None and review.error is None:
        review.error = f"no review after {review.iterations} iterations"
    if isinstance(final, dict):
        review.verdict = "DISAGREE" if str(final.get("verdict", "")).upper() == "DISAGREE" else "AGREE"
        conf = str(final.get("confidence", "low")).lower()
        review.confidence = conf if conf in ("high", "medium", "low") else "low"
        review.vetoes = [v for v in (final.get("vetoes") or []) if isinstance(v, dict)]
        review.proposals = [p for p in (final.get("proposals") or []) if isinstance(p, dict)]
        review.notes = str(final.get("notes", ""))[:1000]
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model, "purpose": "review", "cached": False,
             "input_tokens": usage_in, "output_tokens": usage_out, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_write, "latency_s": round(time.time() - t0, 1),
             "case_id": case.case_id, "iterations": review.iterations, "tool_calls": review.tool_calls}
    _ledger(entry)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"review": review.as_dict(), "usage": entry, "transcript": transcript}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return review


# --------------------------------------------------------------------------- code-side validation of the review
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def validate_review(case: CaseRequest, result, review: Review) -> Review:
    """Accept only vetoes / proposals whose cited source really says what the model claims."""
    manifest = {e.file_id: e for e in load_evidence_manifest()}
    subject = result.subject
    findings = {s.resource_id: s for s in result.steps if s.phase == "FINDING"}
    for v in review.vetoes:
        why = None
        rid, ev_id, src, quote = (str(v.get(k, "")) for k in ("resource_id", "evidence_id", "source", "quote"))
        if rid not in findings:
            why = "no such finding in the draft"
        elif ev_id not in manifest:
            why = "unknown evidence id"
        elif manifest[ev_id].captured_at > case.audit_at:
            why = "evidence captured after audit_at"
        else:
            read = tool_read_source(ev_id, src)
            text = _norm(read.get("text", ""))
            if read.get("error"):
                why = read["error"]
            elif rid.lower() not in text:
                why = "source does not mention the resource"
            elif len(_norm(quote)) < 12 or _norm(quote) not in text:
                why = "quote not found verbatim in the source"
            else:
                op = findings[rid].operation
                if not any(re.search(p, text) for p in COMPLIANCE_MARKERS.get(op, [])):
                    why = "source carries no compliance marker for this finding"
        if why:
            review.rejected.append({"type": "veto", **v, "rejected_because": why})
        else:
            review.accepted_vetoes.append(v)
    for p in review.proposals:
        why = None
        rid, ev_id, src, quote = (str(p.get(k, "")) for k in ("resource_id", "evidence_id", "source", "quote"))
        if not re.fullmatch(r"[a-z_]+_\d+", rid) or rid.startswith("user_"):
            why = "not a resource id"
        elif rid in result.links:
            why = "already in the draft"
        elif ev_id not in manifest:
            why = "unknown evidence id"
        else:
            read = tool_read_source(ev_id, src)
            text = _norm(read.get("text", ""))
            toks = extract_tokens(text)
            if read.get("error"):
                why = read["error"]
            elif rid not in text:
                why = "source does not mention the resource"
            elif not (subject and (subject.match_tokens(toks) or re.search(r"\b" + re.escape(subject.name.lower()) + r"\b", text))):
                why = "source does not name the subject"
            elif len(_norm(quote)) < 12 or _norm(quote) not in text:
                why = "quote not found verbatim in the source"
        if why:
            review.rejected.append({"type": "proposal", **p, "rejected_because": why})
        else:
            review.accepted_proposals.append(p)
    _log({"case_id": case.case_id, "verdict": review.verdict, "confidence": review.confidence, "iterations": review.iterations,
          "tool_calls": review.tool_calls, "cached": review.cached, "error": review.error,
          "vetoes": len(review.vetoes), "accepted_vetoes": len(review.accepted_vetoes),
          "proposals": len(review.proposals), "accepted_proposals": len(review.accepted_proposals),
          "rejected": review.rejected, "notes": review.notes})
    return review


def needs_review(result) -> List[str]:
    """Why a case is uncertain enough to deserve the reviewer (empty list = confident)."""
    reasons = []
    if result.deletion_status == "NOT_APPLICABLE":
        return reasons
    if result.intent and result.intent.get("confidence") != "high":
        reasons.append("intent not high-confidence")
    if not any(r.startswith("account_") for r in result.links):
        reasons.append("no account linked")
    if result.deletion_status == "COMPLETE":
        for rid, link in result.links.items():
            if link.confidence == "run":   # a wrong account becomes a wrongly cited verification step
                reasons.append(f"{rid} linked by deletion-worker timing")
                break
        if any(st.presence == "UNKNOWN" for st in result.states.values()):
            reasons.append("COMPLETE with an unproven resource")
    if any(st.chosen and ".png" in st.chosen.source for st in result.states.values()):
        reasons.append("screenshot evidence")
    for rid, st in result.states.items():   # replication drift: copies disagree at the deciding instant
        if st.presence == "PRESENT" and st.chosen:
            same = {o.presence for o in st.history if o.observed_at == st.observed_at and not o.implicit}
            if len(same) > 1:
                reasons.append(f"{rid}: replica copies disagree")
                break
    return reasons
