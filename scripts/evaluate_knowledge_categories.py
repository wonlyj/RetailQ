"""Run actual configured classification, then require reasoned human review."""
import argparse
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.knowledge.qa_gateway import OpenAIQAGateway, prompt
from retailq.knowledge.review_classification import classify_groups
from retailq.knowledge.mining import atomic_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / 'evals/ch03_console_categories.jsonl'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def load_cases(path):
    cases = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    ids = [c['id'] for c in cases]
    if len(cases) < 12 or len(set(ids)) != len(ids):
        raise ValueError('at least 12 unique labeled cases required')
    for case in cases:
        if (not isinstance(case['id'],str) or not case['id'] or not case['questions'] or
            any(not isinstance(q,str) or not q for q in case['questions']) or not case['answer'] or
            not case['expectation'].get('reason')):
            raise ValueError('complete question/answer/reference reason required')
        extra, repeat = case.pop('catalog_extra',0), case.pop('answer_repeat',1)
        if type(extra) is not int or not 0 <= extra <= 1000 or type(repeat) is not int or not 1 <= repeat <= 1000:
            raise ValueError('case expansion exceeds bounded fixture size')
        case['catalog'] = case['catalog'] + [f'目录样例{i}：商品资料和适用条件' for i in range(extra)]
        case['answer'] *= repeat
    return cases


def current_rules():
    return digest({'prompt':prompt('qa_category.txt'),
        'classification':(ROOT / 'retailq/knowledge/review_classification.py').read_text(encoding='utf-8'),
        'schema':(ROOT / 'retailq/knowledge/review_contracts.py').read_text(encoding='utf-8')})


def initial_report(cases, settings, cases_path):
    return {'schema_version':1, 'case_ids':[c['id'] for c in cases], 'cases':cases,
        'cases_hash':digest(cases), 'cases_path':str(Path(cases_path).resolve()), 'prompt':prompt('qa_category.txt'),
        'prompt_hash':digest(prompt('qa_category.txt')), 'rules_hash':current_rules(),
        'configured_model':settings.openai_model, 'configured_endpoint':settings.openai_base_url,
        'model_identity_note':'模型名为本地配置值，未独立证明远端部署身份。',
        'input_budget':settings.input_budget, 'run_completed':False, 'results':[]}


def evaluate_cases(cases, settings, gateway, *, cases_path=DEFAULT_CASES, checkpoint=None):
    report=initial_report(cases,settings,cases_path)
    if checkpoint:
        checkpoint(report)
    for case in cases:
        row = {'case_id':case['id'], 'questions':case['questions'], 'answer':case['answer'],
            'catalog':case['catalog'], 'expectation':case['expectation'],
            'suggestion':None, 'catalog_truncated':False, 'error':None,
            'review':{'status':'pending','reason':'尚未逐例人工审核分类、完整条件与拒绝建议。'}}
        group = SimpleNamespace(group_id=hashlib.sha256(case['id'].encode()).hexdigest()[:32],
                                questions=tuple(case['questions']), answer=case['answer'])
        seen = []
        class Observed:
            def classify(self, candidates):
                seen.extend(candidates)
                return gateway.classify(candidates)
        try:
            suggestion = classify_groups([group],case['catalog'],Observed(),
                input_budget=min(settings.input_budget,case.get('input_budget',settings.input_budget)),stop=Event())[0]
            row['suggestion'] = suggestion.model_dump(mode='json')
        except Exception as exc:
            row['error'] = {'code':exc.code if isinstance(exc,ServiceError) else 'category_evaluation_failed',
                            'message':exc.public_message if isinstance(exc,ServiceError) else '该例未确认完成，请检查配置。'}
        row['catalog_truncated'] = any(c.catalog_truncated for c in seen) or bool(case['catalog'] and not seen)
        row['sent_to_model'] = bool(seen)
        row['provided_catalog'] = list(seen[0].categories) if seen else []
        report['results'].append(row)
        if checkpoint:
            checkpoint(report)
    report['run_completed'] = all(r['error'] is None for r in report['results'])
    if checkpoint:
        checkpoint(report)
    return report


def summarize_category_report(report):
    """Never compare label strings to auto-approve a semantic result."""
    source_cases = load_cases(report['cases_path'])
    if (report['schema_version'] != 1 or report['rules_hash'] != current_rules() or
        report['prompt_hash'] != digest(prompt('qa_category.txt')) or
        report['prompt'] != prompt('qa_category.txt') or report['cases_hash'] != digest(report['cases']) or
        report['cases_hash'] != digest(source_cases)):
        raise ValueError('report rules/prompt/cases hash mismatch; rerun into a new directory')
    expected = [c['id'] for c in report['cases']]
    ids = [r['case_id'] for r in report['results']]
    if (len(expected) < 12 or len(set(expected)) != len(expected) or report['case_ids'] != expected
            or len(set(ids)) != len(ids) or set(ids) - set(expected)):
        raise ValueError('duplicate, additional or inconsistent case IDs')
    references = {c['id']:c for c in report['cases']}
    statuses = []
    for row in report['results']:
        source = references[row['case_id']]
        if any(row[name] != source[name] for name in ('questions','answer','catalog','expectation')):
            raise ValueError('reported question/answer/reference differs from frozen cases')
        review = row['review']
        if review['status'] not in ('pass','fail','pending') or not isinstance(review.get('reason'),str) or not review['reason'].strip():
            raise ValueError('every review needs status and a concrete reason')
        if review['status'] == 'pass' and (row['suggestion'] is None or row['error']):
            raise ValueError('cannot pass a missing/error suggestion')
        statuses.append(review['status'])
    status = ('fail' if 'fail' in statuses or any(r['error'] for r in report['results']) else
              'pending' if set(ids) != set(expected) or report['run_completed'] is not True or
              'pending' in statuses else 'pass')
    return {'status':status, 'expected':len(expected),'reviewed':sum(s != 'pending' for s in statuses),
            'missing':sorted(set(expected)-set(ids)), 'run_completed':report['run_completed']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',default=str(DEFAULT_CASES))
    parser.add_argument('--output')
    parser.add_argument('--review')
    args = parser.parse_args(argv)
    if args.review:
        try:
            summary = summarize_category_report(json.loads(Path(args.review).read_text(encoding='utf-8')))
            print(json.dumps(summary,ensure_ascii=False,indent=2))
            return {'pass':0,'fail':1,'pending':2}[summary['status']]
        except (OSError,ValueError,KeyError,TypeError):
            print('报告缺失、不完整或规则不匹配，不能判定通过。')
            return 2
    settings = Settings()
    if not args.output or not settings.is_ready:
        print('需要全新--output目录和本地OPENAI_*配置；未发起模型调用。')
        return 2
    output = Path(args.output).resolve()
    if not output.is_relative_to(ROOT / 'artifacts') or output.exists():
        print('输出必须是artifacts下的全新目录；不覆盖旧报告。')
        return 2
    try:
        cases = load_cases(args.cases)
        output.mkdir(parents=True)
        def checkpoint(report):
            atomic_json(output/'report.json',report)
        # Keep the frozen complete case manifest even if gateway setup fails.
        checkpoint(initial_report(cases,settings,args.cases))
        with OpenAIQAGateway(settings) as gateway:
            report = evaluate_cases(cases,settings,gateway,cases_path=args.cases,checkpoint=checkpoint)
        print(f'结果：{output / "report.json"}；逐例填写review.status/reason后运行--review。')
        return 1 if any(r['error'] for r in report['results']) else 2
    except KeyboardInterrupt:
        print('评估已中断；report.json保留已完成用例，run_completed=false，仍需完整重测与人工评审。')
        return 2
    except (ValueError,OSError,ServiceError):
        print('评估未确认完成；保留输出目录，请检查配置或样例。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
