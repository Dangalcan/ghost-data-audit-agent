"""Paths, flags and shared vocabularies."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
CODE_DIR = REPO_ROOT / "code"
CACHE_DIR = CODE_DIR / "cache"
VISION_CACHE_DIR = CACHE_DIR / "vision"
LLM_CACHE_DIR = CACHE_DIR / "llm"
PROMPTS_DIR = CODE_DIR / "audit" / "prompts"
OUTPUT_PATH = REPO_ROOT / "output.csv"
USAGE_LEDGER_PATH = CODE_DIR / "evaluation" / "usage_ledger.jsonl"

# --- behaviour flags -------------------------------------------------------
# Evidence captured after a case's audit_at may be used ONLY to learn which user
# a resource belongs to (ownership is a static fact). It is never used to decide
# the state at audit_at and is never cited.
USE_LATE_EVIDENCE_FOR_LINKAGE = True
# Deletion-worker activity runs that start inside [requested_at, reported_at + N h]
# are treated as candidates for low-confidence resource linkage.
LINK_WINDOW_AFTER_REPORTED_HOURS = 6

INTENT_MODEL = os.environ.get("AUDIT_INTENT_MODEL", "claude-haiku-5-5")
VISION_MODEL = os.environ.get("AUDIT_VISION_MODEL", "claude-sonnet-5-5")

# --- vocabularies ---------------------------------------------------------
RESOURCE_PREFIX_TO_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
}
RESOURCE_ID_RE = r"\b((?:{})_\d+)\b".format("|".join(sorted(RESOURCE_PREFIX_TO_SYSTEM, key=len, reverse=True)))

PHASE_ORDER = ["FINDING", "REMEDIATION", "VERIFICATION"]
# Operation order inside each phase follows the table in problem_statement.md.
OPERATION_ORDER = [
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]
OPERATION_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT",
    "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED",
    "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}


def phase_of(operation: str) -> str:
    if operation.startswith("REPORT_"):
        return "FINDING"
    if operation.startswith("VERIFY_"):
        return "VERIFICATION"
    return "REMEDIATION"


def system_for_resource(resource_id: str) -> str:
    prefix = resource_id.rsplit("_", 1)[0]
    return RESOURCE_PREFIX_TO_SYSTEM.get(prefix, prefix)
