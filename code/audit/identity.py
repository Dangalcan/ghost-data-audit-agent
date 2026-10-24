"""Resolve the request email to a subject and every identifier it ever had.

The request email may be a current or a historical address (user_audit_log.csv).
email_hash is sha256(lower-cased email); we pre-compute it for every known
email so hash-only records (e.g. analytics contact entries) can be matched.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, Optional, Set, Tuple

from .loaders import load_audit_log, load_identities

EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)(?![\w-])")
HASH_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")
USER_RE = re.compile(r"\b(user_\d+)\b")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")


def norm_email(value: str) -> str:
    return (value or "").strip().lower()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def email_hash(value: str) -> str:
    return hashlib.sha256(norm_email(value).encode()).hexdigest()


@dataclass
class Subject:
    user_id: str
    name: str
    current_email: str
    emails: Set[str] = field(default_factory=set)       # lower-cased, current + historical
    hashes: Set[str] = field(default_factory=set)       # sha256 of every email + recorded hashes
    phones: Set[str] = field(default_factory=set)       # digits only, current + historical
    audit_ids: Dict[str, str] = field(default_factory=dict)  # identifier -> audit_id that recorded it

    @property
    def identifiers(self) -> Set[str]:
        return set(self.emails) | set(self.hashes) | {self.user_id}

    def match_tokens(self, tokens: "Tokens") -> Set[str]:
        """Identifiers of this subject that appear in the extracted tokens."""
        hits: Set[str] = set()
        hits |= self.emails & tokens.emails
        hits |= self.hashes & tokens.hashes
        if self.user_id in tokens.user_ids:
            hits.add(self.user_id)
        hits |= self.phones & tokens.phones
        return hits


@dataclass(frozen=True)
class Tokens:
    emails: frozenset
    hashes: frozenset
    user_ids: frozenset
    phones: frozenset

    def __bool__(self) -> bool:
        return bool(self.emails or self.hashes or self.user_ids or self.phones)

    @property
    def all(self) -> Set[str]:
        return set(self.emails) | set(self.hashes) | set(self.user_ids) | set(self.phones)


def extract_tokens(text: str) -> Tokens:
    low = (text or "").lower()
    return Tokens(
        emails=frozenset(EMAIL_RE.findall(low)),
        hashes=frozenset(HASH_RE.findall(low)),
        user_ids=frozenset(USER_RE.findall(low)),
        phones=frozenset(norm_phone(p) for p in PHONE_RE.findall(text or "")),
    )


def tokens_from_values(values: Iterable[Optional[str]]) -> Tokens:
    """Tokens from structured field values (email, user_id, hash, phone columns)."""
    emails, hashes, users, phones = set(), set(), set(), set()
    for v in values:
        if not v or not isinstance(v, str):
            continue
        s = v.strip()
        low = s.lower()
        if EMAIL_RE.fullmatch(low):
            emails.add(low)
        elif HASH_RE.fullmatch(low):
            hashes.add(low)
        elif USER_RE.fullmatch(low):
            users.add(low)
        elif PHONE_RE.fullmatch(s):
            phones.add(norm_phone(s))
        else:  # free text: fall back to regex extraction
            t = extract_tokens(s)
            emails |= t.emails
            hashes |= t.hashes
            users |= t.user_ids
            phones |= t.phones
    return Tokens(frozenset(emails), frozenset(hashes), frozenset(users), frozenset(phones))


@lru_cache(maxsize=None)
def _index() -> Tuple[Dict[str, Subject], Dict[str, Subject]]:
    by_uid: Dict[str, Subject] = {}
    for ident in load_identities():
        subj = Subject(user_id=ident.user_id, name=ident.name, current_email=norm_email(ident.email))
        subj.emails.add(norm_email(ident.email))
        subj.hashes.add(ident.email_hash.lower())
        subj.hashes.add(email_hash(ident.email))
        if norm_phone(ident.phone):
            subj.phones.add(norm_phone(ident.phone))
        by_uid[ident.user_id] = subj
    for change in load_audit_log():
        subj = by_uid.get(change.user_id)
        if subj is None:
            continue
        for value in (change.old_value, change.new_value):
            if change.field_name == "email":
                subj.emails.add(norm_email(value))
                subj.hashes.add(email_hash(value))
                subj.audit_ids.setdefault(norm_email(value), change.audit_id)
            elif change.field_name == "email_hash":
                subj.hashes.add(value.lower())
                subj.audit_ids.setdefault(value.lower(), change.audit_id)
            elif change.field_name == "phone":
                if norm_phone(value):
                    subj.phones.add(norm_phone(value))
                    subj.audit_ids.setdefault(norm_phone(value), change.audit_id)
    by_email: Dict[str, Subject] = {}
    for subj in by_uid.values():
        for e in subj.emails:
            by_email.setdefault(e, subj)
    for ident in load_identities():  # current emails win over historical ones
        by_email[norm_email(ident.email)] = by_uid[ident.user_id]
    return by_uid, by_email


def resolve_subject(email: str) -> Optional[Subject]:
    return _index()[1].get(norm_email(email))


def subject_by_user_id(user_id: str) -> Optional[Subject]:
    return _index()[0].get(user_id)


def all_subjects() -> Dict[str, Subject]:
    return _index()[0]


@lru_cache(maxsize=None)
def identifier_owner() -> Dict[str, str]:
    """identifier (email / hash / user_id / phone digits) -> user_id, for global ownership lookups."""
    owner: Dict[str, str] = {}
    for uid, subj in all_subjects().items():
        for ident in subj.identifiers | subj.phones:
            owner.setdefault(ident, uid)
    return owner


def owners_of(tokens: Tokens) -> Set[str]:
    owner = identifier_owner()
    return {owner[t] for t in tokens.all if t in owner}
