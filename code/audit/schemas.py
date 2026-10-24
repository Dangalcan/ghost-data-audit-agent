"""JSON schemas enforced at every model boundary.

Model output is untrusted until it validates: the intent label, the screenshot
extraction and the reviewer's structured answer are each checked against one of
these schemas before any code consumes them. The reviewer's tool inputs are
validated against the tool definitions in agent.TOOLS the same way.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["DELETION", "NON_DELETION"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string", "maxLength": 1000},
    },
    "required": ["label"],
    "additionalProperties": True,
}

RECORD_TYPES = [
    "USER_PROFILE",
    "SEARCH_INDEX_ENTRY",
    "ANALYTICS_PROFILE",
    "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT",
    "USER_EXPORT",
    "DATA_FLOW",
    "BACKUP_ARCHIVE",
    "SECURITY_AUDIT_LOG",
    "ACCOUNT_TOMBSTONE",
    "MARKETING_PROFILE",
    "LEGAL_HOLD_RECORD",
    "OBJECT",
    "OTHER",
]

_STRING_LIST = {"type": "array", "items": {"type": ["string", "number"]}}

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "captured_at": {"type": ["string", "null"]},
        "console": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": ["string", "null"]},
                    "record_type": {"type": ["string", "null"]},
                    "system": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "enabled": {"type": ["boolean", "null"]},
                    "restore_allowed": {"type": ["boolean", "null"]},
                    "identifiers": {
                        "type": ["object", "null"],
                        "properties": dict.fromkeys(("emails", "user_ids", "phones", "names", "hashes"), _STRING_LIST),
                        "additionalProperties": True,
                    },
                    "aliases": _STRING_LIST,
                    "timestamps": _STRING_LIST,
                    "notes": {"type": ["string", "null"]},
                },
                "additionalProperties": True,
            },
        },
    },
    "required": ["records"],
    "additionalProperties": True,
}

_CITATION = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        "evidence_id": {"type": "string"},
        "source": {"type": "string"},
        "quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["resource_id", "evidence_id", "source", "quote", "reason"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["AGREE", "DISAGREE"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "vetoes": {"type": "array", "items": _CITATION},
        "proposals": {"type": "array", "items": _CITATION},
        "notes": {"type": "string"},
    },
    "required": ["verdict", "confidence", "vetoes", "proposals", "notes"],
    "additionalProperties": False,
}


class SchemaError(ValueError):
    """Model output that does not satisfy the contract."""


def validate(instance: Any, schema: dict[str, Any], what: str) -> Any:
    """Return `instance` when it satisfies `schema`, else raise SchemaError with the first violation."""
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "<root>"
        raise SchemaError(f"{what}: {first.message} at {where}")
    return instance
