"""Tests for the reviewer's tools and for the code-side validation of its answers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["AUDIT_REVIEW_NOLOG"] = "1"  # keep the final run's review log clean

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.agent import Review, needs_review, run_tool, tool_read_source, validate_review  # noqa: E402
from audit.loaders import load_requests  # noqa: E402
from audit.planner import audit_case  # noqa: E402


def _sample(case_id: str):
    case = next(c for c in load_requests(sample=True) if c.case_id == case_id)
    return case, audit_case(case, use_llm=False)


def test_tools_return_structured_data_and_handle_bad_input():
    out = run_tool("search_evidence", {"identifier": "leila.haddad@yahoo.com"})
    assert out["count"] >= 1 and any(o["resource_id"] == "account_005" for o in out["observations"])
    out = run_tool("lookup_activity", {"resource_id": "account_005"})
    assert out["count"] >= 1 and out["activity"][0]["event_type"]
    out = run_tool("resolve_identity", {"email_or_user_id": "nadia.ionescu@yahoo.com"})
    assert out["found"] and out["user_id"] == "user_10"
    assert "error" in run_tool("read_source", {"evidence_id": "evidence_999", "source": "x:1"})
    assert "error" in run_tool("search_evidence", {})


def test_read_source_resolves_every_pointer_kind():
    line = tool_read_source("evidence_94", "resource_lookup_trace_20260926_103856.log:19")
    assert "account_7566" in line["text"] and "http_status=404" in line["text"]
    rec = tool_read_source(
        "evidence_92", "service_resource_export_20260915_114455.json:account_service.profile_lookups[0]"
    )
    assert "account_3136" in rec["text"]
    row = tool_read_source(
        "evidence_54", "customer_accounts_failover_replica_20260902.sql:customer_profiles_failover_replica[0]"
    )
    assert "account_005" in row["text"]


def test_veto_is_rejected_unless_the_source_proves_it():
    case, result = _sample("case_05")  # account_005 still present in the failover replica
    finding = next(s for s in result.steps if s.phase == "FINDING")
    bad = Review(
        case_id=case.case_id,
        verdict="DISAGREE",
        vetoes=[
            {
                "resource_id": finding.resource_id,
                "evidence_id": "evidence_54",
                "source": "customer_accounts_failover_replica_20260902.sql:customer_profiles_failover_replica[0]",
                "quote": "this text is not in the row",
                "reason": "made up",
            }
        ],
    )
    validate_review(case, result, bad)
    assert not bad.accepted_vetoes and bad.rejected[0]["rejected_because"] == "quote not found verbatim in the source"
    late = Review(
        case_id=case.case_id,
        verdict="DISAGREE",
        vetoes=[
            {
                "resource_id": finding.resource_id,
                "evidence_id": "evidence_02",
                "source": "customer_data_snapshot_bundle_20260917.json:snapshots[0][0]",
                "quote": "account_5684",
                "reason": "later file",
            }
        ],
    )
    validate_review(case, result, late)
    assert late.rejected[0]["rejected_because"] == "evidence captured after audit_at"
    # a real contradiction pattern: the line names the resource, the quote is verbatim and carries a 404 marker
    case3, result3 = _sample("case_03")
    fake_finding_result = result3
    good = Review(
        case_id=case3.case_id,
        verdict="DISAGREE",
        vetoes=[
            {
                "resource_id": "account_7566",
                "evidence_id": "evidence_94",
                "source": "resource_lookup_trace_20260926_103856.log:19",
                "quote": "profile_lookup_completed resource_id=account_7566 rows=0 http_status=404",
                "reason": "lookup returned 404",
            }
        ],
    )
    validate_review(case3, fake_finding_result, good)
    # case_03 has no finding on account_7566, so the veto is rejected for that reason, not for the evidence
    assert good.rejected[0]["rejected_because"] == "no such finding in the draft"


def test_proposal_requires_resource_and_subject_in_the_source():
    case, result = _sample("case_12")
    rev = Review(
        case_id=case.case_id,
        verdict="DISAGREE",
        proposals=[
            {
                "resource_id": "search_9999",
                "evidence_id": "evidence_01",
                "source": "service_events_ap_south_20260915.log:92",
                "quote": "analytics_9914",
                "reason": "x",
            }
        ],
    )
    validate_review(case, result, rev)
    assert rev.rejected and rev.rejected[0]["rejected_because"] == "source does not mention the resource"


def test_needs_review_flags_low_confidence_links():
    case, result = _sample("case_21")  # account linked through deletion-worker timing
    assert any("deletion-worker" in r for r in needs_review(result))
    case, result = _sample("case_11")  # everything linked by identifiers from one readback
    assert needs_review(result) == []
