import pytest

from retailq.app import create_app
from tests.support.servers import run_server
from tests.support.upstream import ControlledUpstream
from tests.support.database import chat_settings as settings_for


def test_eval_scoring_and_incomplete():
    from scripts.evaluate import score_extraction, summarize_report
    case = {"expected": {"order_id": "00123", "request_type": "exchange", "expected_solution": "换成42码"},
            "key_facts": ["42"]}
    actual = {"order_id": "00123", "request_type": "exchange", "expected_solution": "更换 42 码"}
    checks = score_extraction(actual, case)
    assert all(checks.values())
    assert not all(score_extraction({**actual, "order_id": "123"}, case).values())
    assert not all(score_extraction({**actual, "expected_solution": "换成40码"}, case).values())
    report = {"expected_case_ids": ["one"], "run_completed": True,
              "cases": [{"id": "one", "auto_checks": checks, "review": {"status": "pending", "reason": ""}}]}
    assert summarize_report(report)["status"] == "pending"
    report["cases"][0]["review"] = {"status": "pass", "reason": "释义与42码诉求一致"}
    assert summarize_report(report)["status"] == "pass"
    report["cases"][0]["auto_checks"]["order_id"] = False
    assert summarize_report(report)["status"] == "fail"
    report["cases"][0]["review"] = {"status": "fail", "reason": "订单号错误"}
    assert summarize_report(report)["pending_review"] == 0


@pytest.mark.anyio
@pytest.mark.mysql
async def test_smoke_carries_actual_history(test_database):
    from scripts.smoke import run_smoke
    upstream = ControlledUpstream(chunks=["订单", "00123"])
    with run_server(upstream.app) as url, run_server(create_app(settings_for(url))) as api:
        report = await run_smoke(api)
        assert report["status"] == "pass"
        assert report["round1"]["token_events"] == 2
        assert upstream.requests[3][0]["messages"][-2] == {"role": "assistant", "content": "订单00123"}
        assert len(upstream.requests) == 5


@pytest.mark.anyio
@pytest.mark.mysql
async def test_smoke_rejects_truncation_and_error(test_database):
    from scripts.smoke import run_smoke
    upstream = ControlledUpstream(chunks=["00123"], finish_reason="length")
    with run_server(upstream.app) as url, run_server(create_app(settings_for(url))) as api:
        report = await run_smoke(api)
        assert report["status"] == "fail"
        assert upstream.request_count == 2


def test_empty_or_unreviewed_report_cannot_pass():
    from scripts.evaluate import summarize_report
    assert summarize_report({"cases": []})["status"] == "pending"
    assert summarize_report({"cases": [{"auto_checks": {}, "review": {"status": "pass", "reason": ""}}]})["status"] != "pass"


def test_partial_duplicate_and_unavailable_reports_cannot_pass():
    from scripts.evaluate import summarize_report
    first = {"id": "one", "auto_checks": {"api": True}, "review": {"status": "pass", "reason": "核对通过"}}
    second = {**first, "id": "two"}
    report = {"expected_case_ids": ["one", "two"], "run_completed": True, "cases": [first, second]}
    assert summarize_report(report)["status"] == "pass"
    assert summarize_report({**report, "cases": [first]})["status"] != "pass"
    assert summarize_report({**report, "cases": [first, first]})["status"] != "pass"
    assert summarize_report({**report, "run_completed": False})["status"] == "pending"
    assert summarize_report({**report, "unavailable": "model_not_configured"})["status"] != "pass"
    assert summarize_report({"cases": [first]})["status"] != "pass"


@pytest.mark.anyio
async def test_evaluation_persists_manifest_before_unavailable(tmp_path):
    from retailq.config import Settings
    from scripts.evaluate import evaluate_cases, summarize_report
    with run_server(create_app(Settings(_env_file=None))) as api:
        report = await evaluate_cases(api, tmp_path)
    assert len(report.get("expected_case_ids", [])) == 24
    assert report["run_completed"] is False
    assert report["unavailable"] == "model_not_configured"
    assert summarize_report(report)["status"] == "pending"
    assert (tmp_path / "report.json").is_file()
