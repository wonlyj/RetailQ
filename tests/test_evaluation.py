import pytest

from retailq.app import create_app
from tests.support.servers import run_server
from tests.support.upstream import ControlledUpstream
from tests.test_gateway import settings_for


def test_eval_scoring_and_incomplete():
    from scripts.evaluate import score_extraction, summarize_report
    case = {"expected": {"order_id": "00123", "request_type": "exchange", "expected_solution": "换成42码"},
            "key_facts": ["42"]}
    actual = {"order_id": "00123", "request_type": "exchange", "expected_solution": "更换 42 码"}
    checks = score_extraction(actual, case)
    assert all(checks.values())
    assert not all(score_extraction({**actual, "order_id": "123"}, case).values())
    assert not all(score_extraction({**actual, "expected_solution": "换成40码"}, case).values())
    report = {"cases": [{"auto_checks": checks, "review": {"status": "pending", "reason": ""}}]}
    assert summarize_report(report)["status"] == "pending"
    report["cases"][0]["review"] = {"status": "pass", "reason": "释义与42码诉求一致"}
    assert summarize_report(report)["status"] == "pass"
    report["cases"][0]["auto_checks"]["order_id"] = False
    assert summarize_report(report)["status"] == "fail"
    report["cases"][0]["review"] = {"status": "fail", "reason": "订单号错误"}
    assert summarize_report(report)["pending_review"] == 0


@pytest.mark.anyio
async def test_smoke_carries_actual_history():
    from scripts.smoke import run_smoke
    upstream = ControlledUpstream(chunks=["订单", "00123"])
    with run_server(upstream.app) as url, run_server(create_app(settings_for(url))) as api:
        report = await run_smoke(api)
        assert report["status"] == "pass"
        assert report["round1"]["token_events"] == 2
        assert upstream.requests[1][0]["messages"][-2] == {"role": "assistant", "content": "订单00123"}
        assert len(upstream.requests) == 3


@pytest.mark.anyio
async def test_smoke_rejects_truncation_and_error():
    from scripts.smoke import run_smoke
    upstream = ControlledUpstream(chunks=["00123"], finish_reason="length")
    with run_server(upstream.app) as url, run_server(create_app(settings_for(url))) as api:
        report = await run_smoke(api)
        assert report["status"] == "fail"
        assert upstream.request_count == 1


def test_empty_or_unreviewed_report_cannot_pass():
    from scripts.evaluate import summarize_report
    assert summarize_report({"cases": []})["status"] == "pending"
    assert summarize_report({"cases": [{"auto_checks": {}, "review": {"status": "pass", "reason": ""}}]})["status"] != "pass"
