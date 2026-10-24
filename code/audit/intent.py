"""Does request_text authorise deletion?

Two layers: a deterministic guard (strong non-deletion / deletion patterns in the
languages present in the data) and, for everything the guard cannot settle with
confidence, the LLM classifier (prompts/request_intent.md) whose answer is a single
enum. The privacy-intake CLASSIFY_REQUEST rows in system_activity.csv are reported
as a hint only. Request text is untrusted input: it is classified, never followed.
"""
from __future__ import annotations

import re
from typing import Dict

from .activity import non_deletion_intake_accounts
from .loaders import CaseRequest

DELETE_RE = re.compile(
    r"delet|eras|remov(e|al) my (account|profile|data)|clos(e|ing) (my|this|the) account|permanent|wipe|"
    r"elimin|borr|suprim|supprim|suppression|exclus[aã]o|apag|l[öo]sch|cancel(a|l)(r|ar|ation)|"
    r"削除|抹消|삭제|удал|حذف|löschung|verwijder|radier",
    re.I)
NON_DELETE_RE = re.compile(
    r"not (a )?(request|asking) (to|for) (erase|delet|remov)|not asking for deletion|"
    r"(do not|don't|please do not) (delete|close|remove|change)|without removing|"
    r"(keep|leave) (the |my )?(account|profile)( as it is| active| and| in place)?|still (use|need) (it|the account|my account)|"
    r"continue using|information request|portable format|download my account information|"
    r"copy of (last year's|the information)|unsubscribe|opt[- ]?out|marketing lists only|promotional emails|"
    r"reset(ting)? (the |my )?(login|password)|misspelled|out of date|change the email address|replace it with the new",
    re.I)


def guard(text: str) -> Dict[str, str]:
    has_del = bool(DELETE_RE.search(text))
    has_non = bool(NON_DELETE_RE.search(text))
    if has_non and not re.search(r"\b(delete|erase|remove) (my|the|this) (account|profile)\b", text, re.I):
        return {"label": "NON_DELETION", "confidence": "high" if not has_del else "medium", "reason": "non-deletion pattern"}
    if has_del and not has_non:
        return {"label": "DELETION", "confidence": "high", "reason": "deletion pattern"}
    return {"label": "DELETION" if has_del else "UNSURE", "confidence": "low", "reason": "mixed/none"}


def classify_intent(case: CaseRequest, use_llm: bool = True) -> Dict[str, str]:
    g = guard(case.request_text)
    hint = ""
    accounts = non_deletion_intake_accounts()
    if accounts:
        hint = "intake rows exist"
    if g["confidence"] == "high" and not use_llm:
        return {**g, "source": "guard"}
    if use_llm:
        try:
            from .llm import classify_request_text
            verdict = classify_request_text(case.request_text)
            if verdict:
                if g["confidence"] == "high" and verdict["label"] != g["label"]:
                    # disagreement: the deterministic guard is conservative, keep the model's view but flag it
                    return {**verdict, "source": "llm", "guard": g["label"], "note": "guard disagreed"}
                return {**verdict, "source": "llm", "guard": g["label"]}
        except Exception as exc:  # no key / network: fall back to the guard
            return {**g, "source": f"guard (llm unavailable: {type(exc).__name__})"}
    if g["label"] == "UNSURE":
        return {"label": "DELETION", "confidence": "low", "reason": "default when unsure", "source": "guard"}
    return {**g, "source": "guard"}
