"""Does request_text authorise deletion?

Two layers: a deterministic guard (strong non-deletion / deletion patterns in the
languages present in the data) and the LLM classifier (prompts/request_intent.md) whose
answer is a schema-validated enum. The model is primary when available; the guard is the
fallback (no credentials, network failure, schema violation) and a cross-check whose
disagreement is recorded on the verdict. Request text is untrusted input: it is
classified, never followed.
"""

from __future__ import annotations

import re

from . import log
from .loaders import CaseRequest

logger = log.get("intent")

DELETE_RE = re.compile(
    r"delet|eras|remov(e|al) my (account|profile|data)|clos(e|ing) (my|this|the) account|permanent|wipe|"
    r"elimin|borr|suprim|supprim|suppression|exclus[aã]o|apag|l[öo]sch|cancel(a|l)(r|ar|ation)|"
    r"削除|抹消|삭제|удал|حذف|löschung|verwijder|radier",
    re.I,
)
NON_DELETE_RE = re.compile(
    r"not (a )?(request|asking) (to|for) (erase|delet|remov)|not asking for deletion|"
    r"(do not|don't|please do not) (delete|close|remove|change)|without removing|"
    r"(keep|leave) (the |my )?(account|profile)( as it is| active| and| in place)?|still (use|need) (it|the account|my account)|"
    r"continue using|information request|portable format|download my account information|"
    r"copy of (last year's|the information)|unsubscribe|opt[- ]?out|marketing lists only|promotional emails|"
    r"reset(ting)? (the |my )?(login|password)|misspelled|out of date|change the email address|replace it with the new",
    re.I,
)
EXPLICIT_DELETE_RE = re.compile(r"\b(delete|erase|remove) (my|the|this) (account|profile)\b", re.I)


def guard(text: str) -> dict[str, str]:
    """Deterministic verdict: DELETION / NON_DELETION / UNSURE with a confidence."""
    has_del = bool(DELETE_RE.search(text))
    has_non = bool(NON_DELETE_RE.search(text))
    if has_non and not EXPLICIT_DELETE_RE.search(text):
        confidence = "medium" if has_del else "high"
        return {"label": "NON_DELETION", "confidence": confidence, "reason": "non-deletion pattern"}
    if has_del and not has_non:
        return {"label": "DELETION", "confidence": "high", "reason": "deletion pattern"}
    return {"label": "DELETION" if has_del else "UNSURE", "confidence": "low", "reason": "mixed/none"}


def _guard_verdict(g: dict[str, str], source: str = "guard") -> dict[str, str]:
    if g["label"] == "UNSURE":  # a deletion workflow was run: audit it unless the text says otherwise
        return {"label": "DELETION", "confidence": "low", "reason": "default when unsure", "source": source}
    return {**g, "source": source}


def classify_intent(case: CaseRequest, use_llm: bool = True) -> dict[str, str]:
    """Intent verdict for one case: model first (validated), deterministic guard as fallback."""
    g = guard(case.request_text)
    if not use_llm:
        return _guard_verdict(g)
    try:
        from .llm import classify_request_text

        verdict = classify_request_text(case.request_text)
    except Exception as exc:  # no key / network / schema violation: the guard decides
        logger.warning(log.kv(case=case.case_id, intent_fallback=type(exc).__name__))
        return _guard_verdict(g, source=f"guard (llm unavailable: {type(exc).__name__})")
    out = {**verdict, "source": "llm", "guard": g["label"]}
    if g["confidence"] == "high" and verdict["label"] != g["label"]:
        out["note"] = "guard disagreed"  # the model's view is kept; the disagreement feeds needs_review
    return out
