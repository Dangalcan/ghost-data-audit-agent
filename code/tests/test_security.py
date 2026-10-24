"""Security guardrails: evidence SQL cannot reach the file system; tool pointers stay inside the dataset."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["AUDIT_REVIEW_NOLOG"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.agent import tool_read_source  # noqa: E402
from audit.evidence.sql import parse_sql  # noqa: E402

GOOD = """PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE customer_profiles (resource_id TEXT, user_id TEXT, email TEXT, updated_at TEXT);
INSERT INTO customer_profiles VALUES ('account_001', 'user_01', 'a@example.com', '2026-09-01T00:00:00Z');
COMMIT;
"""

UNSAFE = {
    "attach.sql": "CREATE TABLE t(a);\nATTACH DATABASE 'leak.db' AS x;\n",
    "vacuum.sql": "CREATE TABLE t(a);\nVACUUM INTO 'leak.db';\n",
    "pragma.sql": "PRAGMA writable_schema=ON;\nCREATE TABLE t(a);\n",
    "hidden.sql": "CREATE TABLE t(a); /* c */ ATTACH DATABASE 'leak.db' AS x;\n",  # caught by the authorizer
}


def test_regular_snapshot_still_parses(tmp_path):
    p = tmp_path / "snapshot.sql"
    p.write_text(GOOD, encoding="utf-8")
    parsed = parse_sql(p, "evidence_t1", "2026-09-02T00:00:00Z")
    assert not parsed.notes
    assert [o.resource_id for o in parsed.observations] == ["account_001"]


def test_sql_file_access_statements_are_rejected(tmp_path):
    for name, script in UNSAFE.items():
        p = tmp_path / name
        p.write_text(script, encoding="utf-8")
        parsed = parse_sql(p, "evidence_bad", "2026-09-02T00:00:00Z")
        assert parsed.observations == [], name
        assert parsed.notes and parsed.notes[0].startswith("rejected"), name
    assert not (tmp_path / "leak.db").exists()
    assert not Path("leak.db").exists()


def test_tool_pointers_cannot_leave_the_dataset():
    assert "error" in tool_read_source("../../.env", "x:1")
    hit = tool_read_source("evidence_94", "..\\..\\.env:1")  # the name part of a pointer never selects a file
    assert "error" not in hit
    assert "ANTHROPIC_API_KEY" not in hit["text"]
