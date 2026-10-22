import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from retailq.config import Settings
from scripts.client import ApiFailure, chat, extract

ROOT = Path(__file__).resolve().parents[1]


def score_extraction(actual: dict, case: dict) -> dict[str, bool]:
    expected = case["expected"]
    solution = actual.get("expected_solution")
    normalized = "".join((solution or "").split()).casefold()
    return {
        "order_id": actual.get("order_id") == expected["order_id"],
        "request_type": actual.get("request_type") == expected["request_type"],
        "solution_presence": solution is None if expected["expected_solution"] is None else bool(solution),
        "key_facts": all("".join(fact.split()).casefold() in normalized for fact in case.get("key_facts", [])),
    }


def summarize_report(report: dict) -> dict:
    cases = report.get("cases", [])
    expected_ids = report.get("expected_case_ids", [])
    actual_ids = [case.get("id") for case in cases]
    execution_complete = bool(
        expected_ids and len(expected_ids) == len(set(expected_ids))
        and len(actual_ids) == len(set(actual_ids))
        and set(actual_ids) == set(expected_ids)
        and report.get("run_completed") is True and not report.get("unavailable")
    )
    failed = sum(1 for case in cases if not all(case.get("auto_checks", {}).values())
                 or case.get("review", {}).get("status") == "fail")
    pending = sum(1 for case in cases if not case.get("auto_checks")
                  or case.get("review", {}).get("status") not in ("pass", "fail")
                  or not case.get("review", {}).get("reason", "").strip())
    status = "fail" if failed else "pending" if pending or not execution_complete else "pass"
    return {"status": status, "total": len(cases), "expected_total": len(expected_ids),
            "failed": failed, "pending_review": pending, "execution_complete": execution_complete}


async def evaluate_cases(base_url: str, output_dir: Path) -> dict:
    settings = Settings()
    dataset = [(kind, json.loads(line))
               for kind, filename in [("chat", "chat_cases.jsonl"), ("extraction", "after_sales_cases.jsonl")]
               for line in (ROOT / "evals" / filename).read_text(encoding="utf-8").splitlines()]
    expected_ids = [case["id"] for _, case in dataset]
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        raise ValueError("evaluation dataset needs unique case IDs")
    report = {"schema_version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
              "configured_model": settings.openai_model, "structured_output_method": settings.structured_output_method,
              "model_identity_source": "local_config_not_independently_verified", "cases": [],
              "expected_case_ids": expected_ids, "run_completed": False}
    output_dir.mkdir(parents=True, exist_ok=True)

    def save():
        report.update(summarize_report(report))
        (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    async with httpx.AsyncClient(trust_env=False, timeout=settings.model_timeout_seconds + 15) as client:
        for kind, case in dataset:
            item = {"id": case["id"], "kind": kind, "input": case, "rubric": case["rubric"],
                    "review": {"status": "pending", "reason": ""}}
            try:
                if kind == "chat":
                    actual = await chat(client, base_url, case["message"], case["history"])
                    checks = {"stream_completed": True, "nonempty_reply": bool(actual["text"])}
                else:
                    actual = await extract(client, base_url, case["description"])
                    checks = score_extraction(actual, case)
                item.update(actual=actual, auto_checks=checks)
            except ApiFailure as exc:
                item.update(error=exc.code, auto_checks={"api_success": False})
                if exc.code == "model_not_configured":
                    report["unavailable"] = "model_not_configured"
                    item["auto_checks"] = {}
                    report["cases"].append(item)
                    save()
                    return report
            except (httpx.HTTPError, ValueError):
                item.update(error="api_or_response_error", auto_checks={"api_success": False})
            report["cases"].append(item)
            # Save progress per case so a failed or interrupted run is inspectable.
            save()
    report["run_completed"] = True
    save()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("artifacts/evals/ch01"))
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    if args.review:
        report = json.loads(args.review.read_text(encoding="utf-8"))
        summary = summarize_report(report)
        report.update(summary)
        report["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        args.review.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        report = asyncio.run(evaluate_cases(args.base_url, args.output))
        summary = {key: report.get(key) for key in ("status", "total", "failed", "pending_review")}
    print(json.dumps(summary, ensure_ascii=False))
    return {"pass": 0, "fail": 1, "pending": 2}[summary["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
