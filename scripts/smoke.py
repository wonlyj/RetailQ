import argparse
import asyncio
import json
from pathlib import Path

import httpx

from scripts.client import ApiFailure, chat, extract


async def run_smoke(base_url: str) -> dict:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=75) as client:
            question = "我的订单号是00123，鞋子小了，想咨询换货。"
            first = await chat(client, base_url, question, [])
            second = await chat(client, base_url, "刚才我说的订单号是多少？", [
                {"role": "user", "content": question}, {"role": "assistant", "content": first["text"]},
            ])
            result = await extract(client, base_url, "订单00123的鞋子小了，我想换成42码。")
        checks = {"multiple_stream_events": first["token_events"] > 1,
                  "context_order_id": "00123" in second["text"],
                  "structured_order_id": result["order_id"] == "00123",
                  "structured_request_type": result["request_type"] == "exchange",
                  "structured_solution": "42" in (result["expected_solution"] or "")}
        return {"status": "pass" if all(checks.values()) else "fail", "checks": checks,
                "round1": first, "round2": second, "extraction": result}
    except ApiFailure as exc:
        return {"status": "pending" if exc.code == "model_not_configured" else "fail", "error": exc.code}
    except (httpx.HTTPError, ValueError):
        return {"status": "fail", "error": "api_or_response_error"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run_smoke(args.base_url))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return {"pass": 0, "fail": 1, "pending": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
