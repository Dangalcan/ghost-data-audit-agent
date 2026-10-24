"""Fast unit tests for the deterministic core (run: python -m pytest code/tests -q)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.config import OPERATION_ORDER, PHASE_ORDER, system_for_resource  # noqa: E402
from audit.evidence.base import Observation, Scope  # noqa: E402
from audit.evidence.logs import parse_line  # noqa: E402
from audit.identity import Tokens, email_hash, extract_tokens, norm_email, resolve_subject  # noqa: E402
from audit.intent import guard  # noqa: E402
from audit.planner import step_sort_key  # noqa: E402
from audit.policy import Step, plan_resource  # noqa: E402
from audit.state import state_at  # noqa: E402


def tok(*values) -> Tokens:
    from audit.identity import tokens_from_values
    return tokens_from_values(values)


def obs(rid, when, presence="PRESENT", kind=None, ev="evidence_01", captured=None, **kw) -> Observation:
    from audit.evidence.base import default_kind
    return Observation(resource_id=rid, evidence_id=ev, observed_at=when, presence=presence,
                       kind=kind or default_kind(rid), tokens=tok(), captured_at=captured or when, **kw)


# --------------------------------------------------------------------------- identity
def test_historical_email_resolves_to_same_user():
    cur = resolve_subject("nadia.petrescu@yahoo.com")
    old = resolve_subject("NADIA.IONESCU@yahoo.com")
    assert cur is not None and old is not None and cur.user_id == old.user_id == "user_10"
    assert email_hash("nadia.ionescu@yahoo.com") in old.hashes


def test_token_extraction_uses_word_boundaries_and_case_folding():
    t = extract_tokens("lookup_key=K.APPIAH88@gmail.com user_4301 user_43 g.silva76.jr@gmail.com")
    assert "k.appiah88@gmail.com" in t.emails and "g.silva76@gmail.com" not in t.emails
    assert {"user_43", "user_4301"} == set(t.user_ids)
    assert norm_email(" A@B.com ") == "a@b.com"


# --------------------------------------------------------------------------- log dialects
def test_log_dialects_parse_to_common_shape():
    lines = {
        "kv": '2026-09-15T08:51:57Z level=INFO service=search_service trace_id=tr-1 resource_id=search_1874 event=index_query_completed result=hit',
        "json": '2026-09-10T08:56:20Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-2","resource_id":"account_6633","event":"profile_lookup_completed","rows":"1","http_status":"200"}',
        "syslog": '2026-08-22T10:51:52Z <14>1 node-2 account_service tr-3 - - level=INFO resource_id="account_9580" event="profile_lookup_completed" rows="0" http_status="404"',
        "pipe": '2026-07-03T07:52:15Z | INFO | account_service | tr-4 | profile_lookup_completed | resource_id="account_4512" rows="0" http_status="404"',
        "bracket": '2026-07-28T05:51:41Z [WARN] [support_platform] [tr-5] message_pii_scan: resource_id="support_2322" matched_value="menon.arjun@gmail.com" detector="direct_identifier"',
        "cloud": '2026-09-30T11:51:03Z requestId=tr-6 level=DEBUG function=profile_service message="resource_id=\\"account_66777\\" event=\\"profile_lookup_completed\\" rows=\\"1\\" http_status=\\"200\\" correlation_key=\\"a.watanabe06@yahoo.com\\""',
    }
    parsed = {k: parse_line(v, 1) for k, v in lines.items()}
    assert parsed["kv"].event == "index_query_completed" and parsed["kv"].kv["resource_id"] == "search_1874"
    assert parsed["json"].trace == "tr-2" and parsed["json"].kv["http_status"] == "200"
    assert parsed["syslog"].trace == "tr-3" and parsed["syslog"].kv["http_status"] == "404"
    assert parsed["pipe"].event == "profile_lookup_completed" and parsed["pipe"].kv["resource_id"] == "account_4512"
    assert parsed["bracket"].kv["matched_value"] == "menon.arjun@gmail.com"
    assert parsed["cloud"].kv["correlation_key"] == "a.watanabe06@yahoo.com" and parsed["cloud"].trace == "tr-6"


# --------------------------------------------------------------------------- state
def test_latest_usable_observation_wins_and_late_files_are_ignored():
    hist = [obs("search_1", "2026-09-01T00:00:00Z", "PRESENT", pii=True),
            obs("search_1", "2026-09-10T00:00:00Z", "ABSENT", ev="evidence_02"),
            obs("search_1", "2026-09-12T00:00:00Z", "PRESENT", ev="evidence_03", captured="2026-09-20T00:00:00Z", pii=True)]
    st = state_at("search_1", "2026-09-15T00:00:00Z", hist, [])
    assert st.presence == "ABSENT" and st.evidence_id == "evidence_02"


def test_replica_drift_counts_as_present():
    same_time = "2026-09-02T23:00:00Z"
    hist = [obs("account_5", same_time, "ABSENT", replica="primary"),
            obs("account_5", same_time, "PRESENT", replica="failover_replica", pii=True)]
    st = state_at("account_5", "2026-09-08T00:00:00Z", hist, [])
    assert st.presence == "PRESENT" and st.pii is True


def test_scoped_snapshot_implies_absence_but_not_for_retained_records():
    scope = Scope(system_id="account_service", evidence_id="evidence_93", observed_at="2026-09-16T08:37:39Z",
                  present={"account_9079"}, captured_at="2026-09-16T08:37:39Z")
    st = state_at("account_3960", "2026-09-16T09:00:00Z", [], [scope])
    assert st.presence == "ABSENT" and st.evidence_id == "evidence_93"
    audit_scope = Scope("security_service", "evidence_93", "2026-09-16T08:37:39Z", {"audit_record_1"}, "2026-09-16T08:37:39Z")
    st2 = state_at("audit_record_4902", "2026-09-16T09:00:00Z", [obs("audit_record_4902", "2026-09-01T00:00:00Z")], [audit_scope])
    assert st2.presence == "PRESENT"


# --------------------------------------------------------------------------- policy
def _plan(rid, when, presence, **kw):
    st = state_at(rid, "2026-09-30T00:00:00Z", [obs(rid, when, presence, **kw)], [])
    return plan_resource(st)


def test_policy_maps_each_category_to_the_required_treatment():
    assert [s.operation for s in _plan("search_7", "2026-09-01T00:00:00Z", "PRESENT", pii=True).remediation] == ["DELETE_RECORD"]
    assert [s.operation for s in _plan("support_7", "2026-09-01T00:00:00Z", "PRESENT", pii=True).remediation] == ["REDACT_RECORD"]
    assert [s.operation for s in _plan("analytics_7", "2026-09-01T00:00:00Z", "PRESENT", pii=True).remediation] == ["ANONYMIZE_RECORD"]
    hash_only = _plan("analytics_8", "2026-09-01T00:00:00Z", "PRESENT", kind="analytics_contact", pii=True)
    assert [s.operation for s in hash_only.remediation] == ["DELETE_RECORD"]
    attachment = _plan("support_8", "2026-09-01T00:00:00Z", "PRESENT", kind="support_attachment", pii=True)
    assert [s.operation for s in attachment.remediation] == ["DELETE_RECORD"]
    flow = _plan("data_flow_1", "2026-09-01T00:00:00Z", "PRESENT", enabled=True)
    assert [s.operation for s in flow.findings] == ["REPORT_ACTIVE_DATA_FLOW"] and flow.remediation[0].operation == "DISABLE_DATA_FLOW"
    backup = _plan("backup_1", "2026-09-01T00:00:00Z", "PRESENT", restore_allowed=True)
    assert backup.remediation[0].operation == "BLOCK_RESTORE" and backup.verification[0].operation == "VERIFY_RESTORE_BLOCKED"
    retained = _plan("audit_record_1", "2026-09-01T00:00:00Z", "PRESENT", retention_until="2034-01-01T00:00:00Z")
    assert not retained.findings and retained.verification[0].operation == "VERIFY_RECORD_PRESENT"
    tomb = _plan("tombstone_1", "2026-09-01T00:00:00Z", "PRESENT", pii=False)
    assert not tomb.findings and tomb.verification[0].operation == "VERIFY_RECORD_REDACTED"
    tomb_pii = _plan("tombstone_2", "2026-09-01T00:00:00Z", "PRESENT", pii=True)
    assert tomb_pii.findings[0].operation == "REPORT_EXPOSED_PII" and tomb_pii.remediation[0].operation == "REDACT_RECORD"


# --------------------------------------------------------------------------- ordering
def test_step_order_is_phase_then_operation_table_then_system_then_resource():
    steps = [Step("VERIFICATION", "security_service", "audit_record_1", "VERIFY_RECORD_PRESENT", "PRESENT"),
             Step("VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
             Step("REMEDIATION", "search_service", "search_1", "DELETE_RECORD", "ABSENT"),
             Step("REMEDIATION", "integration_service", "data_flow_1", "DISABLE_DATA_FLOW", "DISABLED"),
             Step("FINDING", "support_platform", "support_1", "REPORT_EXPOSED_PII", "PRESENT"),
             Step("FINDING", "analytics_platform", "analytics_1", "REPORT_EXPOSED_PII", "PRESENT"),
             Step("FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT")]
    ordered = sorted(steps, key=step_sort_key)
    assert [s.resource_id for s in ordered] == ["search_1", "analytics_1", "support_1", "data_flow_1", "search_1", "account_1", "audit_record_1"]
    assert PHASE_ORDER == ["FINDING", "REMEDIATION", "VERIFICATION"] and OPERATION_ORDER[4] == "DISABLE_DATA_FLOW"


def test_system_for_resource_vocabulary():
    assert system_for_resource("tombstone_17") == "identity_service"
    assert system_for_resource("data_flow_9") == "integration_service"
    assert system_for_resource("object_19") == "object_storage"


# --------------------------------------------------------------------------- intent guard
def test_intent_guard_handles_clear_cases_in_several_languages():
    assert guard("Please permanently delete the account associated with this email address.")["label"] == "DELETION"
    assert guard("Solicito a exclusão permanente da conta associada a este e-mail.")["label"] == "DELETION"
    assert guard("このメールアドレスに関連するアカウントの完全な削除を依頼します。")["label"] == "DELETION"
    assert guard("I'd like to download my account information in a portable format. Please keep the account as it is.")["label"] == "NON_DELETION"
    assert guard("Please remove me from marketing lists only. I still want receipts and service messages for this account.")["label"] == "NON_DELETION"


# --------------------------------------------------------------------------- end-to-end regression on the public sample
def test_sample_predictions_match_published_answers_closely():
    from evaluation.main import evaluate, load
    root = Path(__file__).resolve().parents[2]
    pred = root / "code" / "evaluation" / "sample_predictions.csv"
    if not pred.exists():
        import pytest
        pytest.skip("run python code/main.py --sample first")
    m = evaluate(load(pred), load(root / "dataset" / "sample_output.csv"))
    assert m["case_accuracy"] == 1.0
    assert m["step_f1"] >= 0.95
