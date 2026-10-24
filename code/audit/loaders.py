"""Typed loaders for the participant-facing CSV files."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List

from .config import DATASET_DIR


@dataclass(frozen=True)
class CaseRequest:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class DeletionRule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: str

    @property
    def ext(self) -> str:
        return self.file_path.rsplit(".", 1)[-1].lower()

    @property
    def name(self) -> str:
        return self.file_path.rsplit("/", 1)[-1]


def _read(name: str) -> List[dict]:
    with open(DATASET_DIR / name, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=None)
def load_requests(sample: bool = False) -> List[CaseRequest]:
    name = "sample_deletion_requests.csv" if sample else "deletion_requests.csv"
    return [CaseRequest(**r) for r in _read(name)]


@lru_cache(maxsize=None)
def load_identities() -> List[Identity]:
    return [Identity(**r) for r in _read("user_identities.csv")]


@lru_cache(maxsize=None)
def load_audit_log() -> List[AuditChange]:
    return [AuditChange(**r) for r in _read("user_audit_log.csv")]


@lru_cache(maxsize=None)
def load_activity() -> List[Activity]:
    return [Activity(**r) for r in _read("system_activity.csv")]


@lru_cache(maxsize=None)
def load_rules() -> List[DeletionRule]:
    return [DeletionRule(**r) for r in _read("deletion_rules.csv")]


@lru_cache(maxsize=None)
def load_evidence_manifest() -> List[EvidenceFile]:
    return [EvidenceFile(**r) for r in _read("evidence_files.csv")]


@lru_cache(maxsize=None)
def load_sample_output() -> Dict[str, dict]:
    return {r["case_id"]: r for r in _read("sample_output.csv")}
