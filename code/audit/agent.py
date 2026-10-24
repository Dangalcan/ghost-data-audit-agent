"""Agentic verification layer: a tool-using reviewer that tries to break each draft verdict.

The deterministic engine (planner.py) stays authoritative. The reviewer (Claude Sonnet 5.5
by default) investigates with four read-only tools, then files a structured review:

  search_evidence(identifier)         observations mentioning an email / user id / hash / resource id
  read_source(evidence_id, source)    the raw line or record behind an observation
  lookup_activity(resource_id)        workflow activity rows for a resource (claims)
  resolve_identity(email_or_user_id)  the subject's current and historical identifiers
  submit_review(...)                  final structured answer (strict schema)

Guardrails
- iteration cap, per-call timeout, typed retries with backoff; malformed tool input -> error result;
- the model can only VETO a finding or PROPOSE an extra resource by citing an evidence id,
  the exact `source` returned by the tools and a verbatim quote. Code re-reads the source
  and accepts the veto only if the quote is there, the resource id is there, the file was
  captured at or before audit_at and the text carries a compliance marker for that finding.
  Proposals are accepted only if the source names both the resource and the subject;
- the final answer is validated against schemas.REVIEW_SCHEMA; an invalid answer is
  recorded as an error and changes nothing (the draft stands);
- all calls are cached (same draft -> same review) and logged in the usage ledger;
- evidence text is untrusted data: the system prompt says so and tool results are JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import llm, log
from .activity import activities_by_resource
from .config import DATASET_DIR, LLM_CACHE_DIR, PROMPTS_DIR
from .identity import Subject, extract_tokens, resolve_subject, subject_by_user_id
from .linkage import observations_by_resource, observations_with_aliases
from .loaders import CaseRequest, EvidenceFile, load_evidence_manifest
from .schemas import REVIEW_SCHEMA, SchemaError, validate

if TYPE_CHECKING:
    from .evidence.base import Observation
    from .planner import CaseResult
    from .policy import Step
    from .state import ResourceState

logger = log.get("agent")
REVIEW_MODEL = os.environ.get("AUDIT_REVIEW_MODEL", "claude-sonnet-5-5")
MAX_ITERATIONS = int(os.environ.get("AUDIT_REVIEW_MAX_ITER", "8"))
MAX_TOOL_RESULT_CHARS = 2500
MAX_OUTPUT_TOKENS = 4000
REVIEW_LOG = Path(__file__).resolve().parents[1] / "evaluation" / "review_log.jsonl"
RESOURCE_ID_RE = re.compile(r"[a-z_]+_\d+")

COMPLIANCE_MARKERS = {
    "REPORT_REMAINING_RECORD": [
        r"\b404\b",
        r"not_found",
        r"rows=\"?0\"?",
        r"\babsent\b",
        r"\bdeleted\b",
        r"no longer exists",
        r"download_available\W+false",
        r"status_code\W+404",
        r"\"profile\": null",
    ],
    "REPORT_EXPOSED_PII": [
        r"anonymi[sz]ed",
        r"redacted",
        r"\[redacted\]",
        r"direct_identifiers=\"?0\"?",
        r"email=null",
        r"\"email\": null",
        r"requester_email=null",
        r"no direct identifier",
    ],
    "REPORT_ACTIVE_DATA_FLOW": [r"enabled=\"?false\"?", r"\"enabled\": false", r"\bdisabled\b", r"\bstopped\b", r"'0'"],
    "REPORT_RESTORE_RISK": [
        r"restore_allowed=\"?false\"?",
        r"\"restore_allowed\": false",
        r"\bblocked\b",
        r"decision=\"?deny\"?",
    ],
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return {"name": name, "description": description, "input_schema": schema, "strict": True}


TOOLS: list[dict[str, Any]] = [
    _tool(
        "search_evidence",
        "Find evidence observations mentioning an identifier: an email address, a user id (user_12), "
        "an email hash, a phone number or a resource id (account_12, search_7, analytics_3, support_5, "
        "export_2, data_flow_9, backup_4, audit_record_8, tombstone_1). Returns up to 30 observations "
        "with evidence id, capture time, record time, presence, kind, flags and the source pointer to read.",
        {"identifier": {"type": "string"}},
        ["identifier"],
    ),
    _tool(
        "read_source",
        "Read the raw text behind an observation: the log line, JSON record, SQL row, document text or "
        "screenshot record identified by an evidence id and the exact source pointer returned by search_evidence.",
        {"evidence_id": {"type": "string"}, "source": {"type": "string"}},
        ["evidence_id", "source"],
    ),
    _tool(
        "lookup_activity",
        "Workflow activity rows (deletion worker, sync, index, restore, catalog) for one resource id. "
        "These are claims reported by systems, not proof of state.",
        {"resource_id": {"type": "string"}},
        ["resource_id"],
    ),
    _tool(
        "resolve_identity",
        "Resolve an email address or user id to the person's stable user id and every identifier they ever had.",
        {"email_or_user_id": {"type": "string"}},
        ["email_or_user_id"],
    ),
    {
        "name": "submit_review",
        "description": "File the final review. Call exactly once, after investigating. vetoes: findings of the draft that "
        "a specific line of evidence contradicts. proposals: resources of the subject the draft missed.",
        "input_schema": REVIEW_SCHEMA,
        "strict": True,
    },
]


# --------------------------------------------------------------------------- tool implementations
@cache
def _manifest() -> dict[str, EvidenceFile]:
    return {e.file_id: e for e in load_evidence_manifest()}


def _obs_payload(o: Observation) -> dict[str, Any]:
    return {
        "evidence_id": o.evidence_id,
        "captured_at": o.captured_at,
        "observed_at": o.observed_at,
        "resource_id": o.resource_id,
        "presence": o.presence,
        "kind": o.kind,
        "pii": o.pii,
        "enabled": o.enabled,
        "restore_allowed": o.restore_allowed,
        "replica": o.replica,
        "implicit": o.implicit,
        "source": o.source,
    }


def tool_search_evidence(identifier: str) -> dict[str, Any]:
    ident = identifier.strip().lower()
    if RESOURCE_ID_RE.fullmatch(ident) and not ident.startswith("user_"):
        hits = list(observations_by_resource(False).get(ident, []))
    else:
        wanted = set(extract_tokens(ident).all) | {ident}
        hits = [o for o in observations_with_aliases(False) if o.tokens and (o.tokens.all & wanted)]
    hits = sorted(hits, key=lambda o: o.observed_at)[:30]
    return {"count": len(hits), "observations": [_obs_payload(o) for o in hits]}


def _json_path(data: Any, path: str) -> Any:
    """Navigate 'snapshots[3][1]' / 'records[5]' / 'account_service.profile_lookups[2].documents[0]'."""
    cur = data
    for token in re.findall(r"[A-Za-z_]+|\[\d+\]", path):
        if token.startswith("["):
            if isinstance(cur, dict) and isinstance(cur.get("records"), list):
                cur = cur["records"]
            cur = cur[int(token[1:-1])]
        elif isinstance(cur, dict) and token in cur:
            cur = cur[token]
        elif isinstance(cur, dict) and token in (cur.get("service_exports") or {}):
            cur = cur["service_exports"][token]
        else:
            raise KeyError(token)
    return cur


def _read_log_lines(path: Path, lineno: int) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(0, lineno - 2), min(len(lines), lineno + 1)
    return "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))


def _read_sql_row(path: Path, pointer: str) -> str:
    m = re.match(r"(\w+)\[(\d+)\]", pointer)
    if m is None:
        raise ValueError(f"bad SQL pointer {pointer!r}")
    table, idx = m.groups()
    rows = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(f'INSERT INTO "{table}"') or line.startswith(f"INSERT INTO {table} ")
    ]
    return rows[int(idx)]


def _read_png_record(evidence_id: str, pointer: str) -> str:
    from .evidence.images import cached_extraction

    data = cached_extraction(evidence_id) or {}
    m = re.search(r"records\[(\d+)\]", pointer)
    return json.dumps(data.get("records", [])[int(m.group(1))] if m else data, ensure_ascii=False)


def _derived_text(source: str) -> str | None:
    if source.startswith("alias:"):
        return f"catalog alias match: {source[6:]}"
    if source.startswith(("scope:", "expiry:")):
        return (
            f"derived observation ({source}); the file lists the records of that system and this one is not among them"
        )
    return None


def _raw_text(ev: EvidenceFile, source: str) -> str:
    derived = _derived_text(source)
    if derived is not None:
        return derived
    path = DATASET_DIR / ev.file_path
    _, _, pointer = source.partition(":")
    if ev.ext in ("log", "txt"):
        return _read_log_lines(path, int(pointer))
    if ev.ext == "json":
        return json.dumps(_json_path(json.loads(path.read_text(encoding="utf-8")), pointer), ensure_ascii=False)
    if ev.ext == "sql":
        return _read_sql_row(path, pointer)
    if ev.ext == "pdf":
        from .evidence.text import pdf_text

        return pdf_text(path)
    if ev.ext == "png":
        return _read_png_record(ev.file_id, pointer)
    raise ValueError(f"unsupported evidence type {ev.ext}")


def tool_read_source(evidence_id: str, source: str) -> dict[str, Any]:
    ev = _manifest().get(evidence_id)
    if ev is None:
        return {"error": f"unknown evidence id {evidence_id}"}
    try:
        text = _raw_text(ev, source)
    except Exception as exc:  # bad pointer from the model: tell it, do not crash the loop
        return {"error": f"cannot read {source}: {type(exc).__name__}: {exc}"}
    return {"evidence_id": evidence_id, "captured_at": ev.captured_at, "text": text[:MAX_TOOL_RESULT_CHARS]}


def tool_lookup_activity(resource_id: str) -> dict[str, Any]:
    rows = activities_by_resource().get(resource_id.strip(), [])
    keys = ("activity_id", "event_time", "event_type", "source_system", "target_system", "reported_result", "details")
    return {"count": len(rows), "activity": [{k: getattr(a, k) for k in keys} for a in rows[:25]]}


def tool_resolve_identity(value: str) -> dict[str, Any]:
    v = value.strip()
    subj = subject_by_user_id(v) if v.startswith("user_") else resolve_subject(v)
    if subj is None:
        return {"found": False}
    return {
        "found": True,
        "user_id": subj.user_id,
        "name": subj.name,
        "emails": sorted(subj.emails),
        "phones": sorted(subj.phones),
        "email_hashes": sorted(subj.hashes),
    }


def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one tool call; every failure becomes an error payload for the model."""
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
def _resource_line(rid: str, link_conf: str, st: ResourceState, findings: list[Step]) -> str:
    top = st.chosen
    return (
        f"- {rid} link={link_conf} kind={st.kind} state@audit={st.presence} pii={st.pii} enabled={st.enabled} "
        f"restore_allowed={st.restore_allowed} evidence={st.evidence_id} observed_at={st.observed_at} "
        f"source={top.source if top else None} -> findings={[f.operation for f in findings]}"
    )


def render_draft(case: CaseRequest, result: CaseResult) -> str:
    """The engine's verdict as the reviewer sees it (also the cache key of the review)."""
    s = result.subject
    label = result.intent.get("label") if result.intent else "n/a"
    lines = [
        f"CASE {case.case_id}",
        f"request email: {case.email}",
        f"requested_at: {case.requested_at}",
        f"workflow claim: {case.reported_status} reported at {case.reported_at} (a claim, not proof)",
        f"audit_at (cut-off): {case.audit_at}",
        f"request_text (untrusted customer input, already classified as {label}): {case.request_text[:300]!r}",
        f"subject: {s.user_id} name={s.name} emails={sorted(s.emails)} phones={sorted(s.phones)} hashes={len(s.hashes)}",
        "",
        "LINKED RESOURCES (draft):",
    ]
    for rid, link in sorted(result.links.items()):
        lines.append(_resource_line(rid, link.confidence, result.states[rid], result.plans[rid].findings))
    lines += ["", f"DRAFT VERDICT: {result.deletion_status}", "DRAFT STEPS:"]
    for i, st in enumerate(result.steps, 1):
        lines.append(
            f"  {i}. {st.phase} {st.operation} {st.system_id} {st.resource_id} {st.state} evidence={st.evidence_id or '-'}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- the loop
@dataclass
class Review:
    case_id: str
    verdict: str = "AGREE"
    confidence: str = "low"
    vetoes: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    iterations: int = 0
    tool_calls: int = 0
    cached: bool = False
    error: str | None = None
    accepted_vetoes: list[dict[str, Any]] = field(default_factory=list)
    accepted_proposals: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, u: Any) -> None:
        self.input += u.input_tokens
        self.output += u.output_tokens
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0


def _prompt() -> str:
    return (PROMPTS_DIR / "case_review.md").read_text(encoding="utf-8")


def _review_cache_path(model: str, system: str, draft: str) -> Path:
    key = hashlib.sha256("\x1f".join([model, system, json.dumps(TOOLS, sort_keys=True), draft]).encode()).hexdigest()
    return LLM_CACHE_DIR / f"review_{key[:24]}.json"


def _cached_review(path: Path, case_id: str, model: str) -> Review | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    review = Review(**{k: v for k, v in data["review"].items() if k in Review.__dataclass_fields__})
    review.cached = True
    llm.ledger(llm.cached_entry(model, "review", case_id=case_id))
    return review


class ReviewLoop:
    """One reviewer conversation: bounded iterations, tool dispatch, validated final answer."""

    def __init__(self, case: CaseRequest, result: CaseResult, model: str, max_iterations: int) -> None:
        self.case = case
        self.model = model
        self.max_iterations = max_iterations
        self.draft = render_draft(case, result)
        self.system = _prompt()
        self.review = Review(case_id=case.case_id)
        self.messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [{"type": "text", "text": self.draft + "\n\nInvestigate, then call submit_review."}],
            }
        ]
        self.transcript: list[dict[str, Any]] = []
        self.usage = _Usage()
        self.final: dict[str, Any] | None = None

    def run(self) -> Review:
        cache_path = _review_cache_path(self.model, self.system, self.draft)
        cached = _cached_review(cache_path, self.case.case_id, self.model)
        if cached is not None:
            return cached
        t0 = time.time()
        for it in range(1, self.max_iterations + 1):
            self.review.iterations = it
            if not self._step(it):
                break
        self._finalize()
        entry = self._usage_entry(t0)
        llm.ledger(entry)
        self._persist(cache_path, entry)
        logger.info(
            log.kv(
                case=self.case.case_id,
                verdict=self.review.verdict,
                iterations=self.review.iterations,
                tool_calls=self.review.tool_calls,
                error=self.review.error,
            )
        )
        return self.review

    def _step(self, it: int) -> bool:
        """One model turn. Returns False when the loop must stop."""
        try:
            response = llm.messages(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": "medium"},
            )
        except llm.ModelRefused:
            self.review.error = "model refused"
            return False
        except Exception as exc:  # no credentials, or API error after the retries
            self.review.error = f"{type(exc).__name__}: {exc}"
            return False
        self.usage.add(response.usage)
        self.messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return self._handle_text(response, it)
        self.messages.append({"role": "user", "content": self._handle_tools(tool_uses, it)})
        return self.final is None

    def _handle_text(self, response: Any, it: int) -> bool:
        """No tool call: accept a JSON answer in text, otherwise nudge once more."""
        text = "".join(b.text for b in response.content if b.type == "text")
        self.transcript.append({"iteration": it, "text": text[:1500]})
        try:
            self.final = llm.extract_json(text)
            return False
        except SchemaError:
            self.messages.append({"role": "user", "content": "Call submit_review with your structured review now."})
            return True

    def _handle_tools(self, tool_uses: list[Any], it: int) -> list[dict[str, Any]]:
        results = []
        for tu in tool_uses:
            self.review.tool_calls += 1
            if tu.name == "submit_review":
                self.final = dict(tu.input or {})
                self.transcript.append({"iteration": it, "tool": "submit_review", "input": tu.input})
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps({"status": "received"})}
                )
                continue
            payload = json.dumps(run_tool(tu.name, tu.input or {}), ensure_ascii=False)
            if len(payload) > MAX_TOOL_RESULT_CHARS * 3:
                payload = payload[: MAX_TOOL_RESULT_CHARS * 3] + "... (truncated)"
            self.transcript.append({"iteration": it, "tool": tu.name, "input": tu.input, "result_chars": len(payload)})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": payload})
        return results

    def _finalize(self) -> None:
        """Validate the structured answer; anything invalid leaves the default AGREE review."""
        r = self.review
        if self.final is None:
            r.error = r.error or f"no review after {r.iterations} iterations"
            return
        answer = {"confidence": "low", "vetoes": [], "proposals": [], "notes": "", **self.final}
        answer["verdict"] = str(answer.get("verdict", "")).upper()
        try:
            validate(answer, REVIEW_SCHEMA, "review")
        except SchemaError as exc:
            r.error = str(exc)
            logger.warning(log.kv(case=self.case.case_id, invalid_review=str(exc)))
            return
        r.verdict, r.confidence = answer["verdict"], answer["confidence"]
        r.vetoes, r.proposals = list(answer["vetoes"]), list(answer["proposals"])
        r.notes = str(answer["notes"])[:1000]

    def _usage_entry(self, t0: float) -> dict[str, Any]:
        return {
            "ts": llm.now_iso(),
            "model": self.model,
            "purpose": "review",
            "cached": False,
            "input_tokens": self.usage.input,
            "output_tokens": self.usage.output,
            "cache_read_input_tokens": self.usage.cache_read,
            "cache_creation_input_tokens": self.usage.cache_write,
            "latency_s": round(time.time() - t0, 1),
            "case_id": self.case.case_id,
            "iterations": self.review.iterations,
            "tool_calls": self.review.tool_calls,
        }

    def _persist(self, cache_path: Path, entry: dict[str, Any]) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"review": self.review.as_dict(), "usage": entry, "transcript": self.transcript}
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def review_case(
    case: CaseRequest, result: CaseResult, model: str = REVIEW_MODEL, max_iterations: int = MAX_ITERATIONS
) -> Review:
    """Run the reviewer loop for one case. Never raises; errors are recorded on the Review."""
    return ReviewLoop(case, result, model, max_iterations).run()


# --------------------------------------------------------------------------- code-side validation of the review
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _fields(item: dict[str, Any]) -> tuple[str, str, str, str]:
    rid, ev_id, src, quote = (str(item.get(k, "")) for k in ("resource_id", "evidence_id", "source", "quote"))
    return rid, ev_id, src, quote


def _source_text(ev_id: str, src: str) -> tuple[str, str | None]:
    read = tool_read_source(ev_id, src)
    return _norm(read.get("text", "")), read.get("error")


def _mentions(text: str, rid: str, quote: str) -> str | None:
    if rid.lower() not in text:
        return "source does not mention the resource"
    if len(_norm(quote)) < 12 or _norm(quote) not in text:
        return "quote not found verbatim in the source"
    return None


def _names_subject(subject: Subject | None, text: str) -> bool:
    if subject is None:
        return False
    if subject.match_tokens(extract_tokens(text)):
        return True
    return re.search(r"\b" + re.escape(subject.name.lower()) + r"\b", text) is not None


def _check_veto(case: CaseRequest, findings: dict[str, Step], v: dict[str, Any]) -> str | None:
    """Why a veto is rejected, or None when the cited source really contradicts the finding."""
    rid, ev_id, src, quote = _fields(v)
    if rid not in findings:
        return "no such finding in the draft"
    ev = _manifest().get(ev_id)
    if ev is None:
        return "unknown evidence id"
    if ev.captured_at > case.audit_at:
        return "evidence captured after audit_at"
    text, error = _source_text(ev_id, src)
    why = error or _mentions(text, rid, quote)
    if why:
        return why
    markers = COMPLIANCE_MARKERS.get(findings[rid].operation, [])
    if not any(re.search(p, text) for p in markers):
        return "source carries no compliance marker for this finding"
    return None


def _check_proposal(result: CaseResult, p: dict[str, Any]) -> str | None:
    """Why a proposal is rejected, or None when the source names both resource and subject."""
    rid, ev_id, src, quote = _fields(p)
    if not RESOURCE_ID_RE.fullmatch(rid) or rid.startswith("user_"):
        return "not a resource id"
    if rid in result.links:
        return "already in the draft"
    if ev_id not in _manifest():
        return "unknown evidence id"
    text, error = _source_text(ev_id, src)
    why = error or _mentions(text, rid, quote)
    if why:
        return why
    if not _names_subject(result.subject, text):
        return "source does not name the subject"
    return None


def _log_review(case_id: str, review: Review) -> None:
    if os.environ.get("AUDIT_REVIEW_NOLOG"):  # tests
        return
    REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "case_id": case_id,
        "verdict": review.verdict,
        "confidence": review.confidence,
        "iterations": review.iterations,
        "tool_calls": review.tool_calls,
        "cached": review.cached,
        "error": review.error,
        "vetoes": len(review.vetoes),
        "accepted_vetoes": len(review.accepted_vetoes),
        "proposals": len(review.proposals),
        "accepted_proposals": len(review.accepted_proposals),
        "rejected": review.rejected,
        "notes": review.notes,
    }
    with open(REVIEW_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def validate_review(case: CaseRequest, result: CaseResult, review: Review) -> Review:
    """Accept only vetoes / proposals whose cited source really says what the model claims."""
    findings = {s.resource_id: s for s in result.steps if s.phase == "FINDING"}
    for v in review.vetoes:
        why = _check_veto(case, findings, v)
        if why:
            review.rejected.append({"type": "veto", **v, "rejected_because": why})
        else:
            review.accepted_vetoes.append(v)
    for p in review.proposals:
        why = _check_proposal(result, p)
        if why:
            review.rejected.append({"type": "proposal", **p, "rejected_because": why})
        else:
            review.accepted_proposals.append(p)
    _log_review(case.case_id, review)
    if review.rejected or review.accepted_vetoes or review.accepted_proposals:
        logger.info(
            log.kv(
                case=case.case_id,
                accepted_vetoes=len(review.accepted_vetoes),
                accepted_proposals=len(review.accepted_proposals),
                rejected=len(review.rejected),
            )
        )
    return review


# --------------------------------------------------------------------------- which cases deserve a review
def _replica_disagreement(states: dict[str, ResourceState]) -> str | None:
    """Replication drift: copies disagree at the deciding instant."""
    for rid, st in states.items():
        if st.presence != "PRESENT" or st.chosen is None:
            continue
        same = {o.presence for o in st.history if o.observed_at == st.observed_at and not o.implicit}
        if len(same) > 1:
            return f"{rid}: replica copies disagree"
    return None


def needs_review(result: CaseResult) -> list[str]:
    """Why a case is uncertain enough to deserve the reviewer (empty list = confident)."""
    if result.deletion_status == "NOT_APPLICABLE":
        return []
    reasons = []
    if result.intent and result.intent.get("confidence") != "high":
        reasons.append("intent not high-confidence")
    if not any(r.startswith("account_") for r in result.links):
        reasons.append("no account linked")
    if result.deletion_status == "COMPLETE":
        timed = [rid for rid, link in result.links.items() if link.confidence == "run"]
        if timed:  # a wrong account becomes a wrongly cited verification step
            reasons.append(f"{timed[0]} linked by deletion-worker timing")
        if any(st.presence == "UNKNOWN" for st in result.states.values()):
            reasons.append("COMPLETE with an unproven resource")
    if any(st.chosen and ".png" in st.chosen.source for st in result.states.values()):
        reasons.append("screenshot evidence")
    drift = _replica_disagreement(result.states)
    if drift:
        reasons.append(drift)
    return reasons
