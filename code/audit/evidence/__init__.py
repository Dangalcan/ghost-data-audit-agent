"""Evidence registry: parse every file in evidence_files.csv once."""

from __future__ import annotations

from functools import cache

from ..config import DATASET_DIR
from ..loaders import EvidenceFile, load_evidence_manifest
from .base import Observation, ParsedEvidence, Scope


def _parser_for(ext: str):
    if ext == "log":
        from .logs import parse_log

        return parse_log
    if ext == "json":
        from .records_json import parse_json

        return parse_json
    if ext == "sql":
        from .sql import parse_sql

        return parse_sql
    if ext == "txt":
        from .text import parse_text

        return parse_text
    if ext == "pdf":
        from .text import parse_pdf

        return parse_pdf
    if ext == "png":
        from .images import parse_image

        return parse_image
    raise ValueError(f"no parser for .{ext}")


def parse_file(ev: EvidenceFile) -> ParsedEvidence:
    path = DATASET_DIR / ev.file_path
    parsed = _parser_for(ev.ext)(path, ev.file_id, ev.captured_at)
    for o in parsed.observations:
        o.captured_at = ev.captured_at
    for sc in parsed.scopes:
        sc.captured_at = ev.captured_at
    return parsed


@cache
def all_evidence(skip_images: bool = False) -> dict[str, ParsedEvidence]:
    out: dict[str, ParsedEvidence] = {}
    for ev in load_evidence_manifest():
        if skip_images and ev.ext == "png":
            out[ev.file_id] = ParsedEvidence(
                evidence_id=ev.file_id, captured_at=ev.captured_at, notes=["skipped image"]
            )
            continue
        out[ev.file_id] = parse_file(ev)
    return out


def all_observations(skip_images: bool = False) -> list[Observation]:
    return [o for pe in all_evidence(skip_images).values() for o in pe.observations]


def all_scopes(skip_images: bool = False) -> list[Scope]:
    return [s for pe in all_evidence(skip_images).values() for s in pe.scopes]


@cache
def file_subjects(skip_images: bool = False) -> dict[str, set]:
    """evidence_id -> user ids mentioned anywhere in that file."""
    from ..identity import owners_of

    out: dict[str, set] = {}
    for o in all_observations(skip_images):
        out.setdefault(o.evidence_id, set()).update(owners_of(o.tokens))
    return out


def scopes_for(user_id: str, skip_images: bool = False) -> list[Scope]:
    """Scopes usable for one subject.

    A snapshot proves absence only for the people it was taken for: the inventory and
    replica captures list the records of the users under review at that time, so a
    record missing from a capture that never mentions the subject proves nothing.
    """
    fs = file_subjects(skip_images)
    return [sc for sc in all_scopes(skip_images) if user_id in fs.get(sc.evidence_id, set())]
