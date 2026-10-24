"""Automatic three-case smoke; full semantic review lives in evaluate_tools."""
import argparse
import asyncio
import json
from pathlib import Path

from scripts.evaluate_tools import evaluate_tool_cases


async def run_tool_smoke(base_url: str, output_dir: Path) -> dict:
    report = await evaluate_tool_cases(base_url,output_dir,
        case_ids=['logistics-1001','faq-return','faq-shipping-gap'])
    checks = {item['id']:bool(item.get('auto_checks')) and all(item['auto_checks'].values()) for item in report['cases']}
    passed = report.get('execution_complete') and len(checks)==3 and all(checks.values())
    return {'status':'pass' if passed else 'pending' if report.get('unavailable') else 'fail',
        'scope':'automatic tool selection / database / stream checks; semantic review remains in evaluation',
        'checks':checks,'cases':report['cases']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,default=Path('artifacts/live/ch02/smoke.json'))
    args = parser.parse_args()
    report = asyncio.run(run_tool_smoke(args.base_url,args.output.parent/'smoke-cases'))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'checks':report['checks']},ensure_ascii=False,indent=2))
    return {'pass':0,'fail':1,'pending':2}[report['status']]


if __name__=='__main__':
    raise SystemExit(main())
