"""Valid JSON or matching a reference label must never auto-pass semantic review."""
import copy
import importlib.util
import json

import pytest


def evaluator():
    assert importlib.util.find_spec('scripts.evaluate_knowledge_categories'), 'category evaluation tool is missing'
    from scripts import evaluate_knowledge_categories
    return evaluate_knowledge_categories


def pending_report():
    from retailq.config import Settings
    from retailq.knowledge.review_contracts import CategorySuggestion
    module = evaluator()
    class Gateway:
        def classify(self, candidates):
            return [CategorySuggestion(group_id=c.group_id, category=c.categories[0] if c.categories else None,
                reason='测试替身，仅验证报告门禁。') for c in candidates]
    return module.evaluate_cases(module.load_cases(module.DEFAULT_CASES),
        Settings(_env_file=None, context_window_tokens=31536), Gateway())


def test_unreviewed_or_incomplete_category_report_cannot_pass():
    module = evaluator()
    report = pending_report()
    assert module.summarize_category_report(report)['status'] == 'pending'
    report['results'].append(copy.deepcopy(report['results'][0]))
    with pytest.raises(ValueError):
        module.summarize_category_report(report)


@pytest.mark.parametrize('change', ['missing','extra','no_reason','incomplete','bad_hash','bad_prompt','rewrite_case_set'])
def test_semantic_review_requires_full_cases_reasons_completion_and_matching_rules(change):
    module = evaluator()
    report = pending_report()
    for row in report['results']:
        row['review'] = {'status':'pass','reason':'已核对完整问题、条件和建议分类，允许此结果。'}
    assert module.summarize_category_report(report)['status'] == 'pass'
    if change == 'missing': report['results'].pop()
    elif change == 'extra': report['results'][0]['case_id'] = 'invented-case'
    elif change == 'no_reason': report['results'][0]['review']['reason'] = ''
    elif change == 'incomplete': report['run_completed'] = False
    elif change == 'bad_hash': report['rules_hash'] = '0'*64
    elif change == 'bad_prompt': report['prompt_hash'] = '0'*64
    else:
        report['cases'].pop()
        report['case_ids'].pop()
        report['results'].pop()
        report['cases_hash'] = module.digest(report['cases'])
    try:
        assert module.summarize_category_report(report)['status'] != 'pass'
    except ValueError:
        pass


def test_labeled_cases_preserve_questions_conditions_and_human_override():
    module = evaluator()
    cases = {c['id']:c for c in module.load_cases(module.DEFAULT_CASES)}
    assert len(cases) >= 12
    assert cases['existing-shipping']['expectation']['category_candidates'] == ['配送规则']
    assert cases['single-order']['expectation']['recommend_reject'] is True
    assert cases['ambiguous']['expectation']['allow_null'] is True
    assert len(cases['synonym-questions']['questions']) == 2
    assert '偏远' in cases['remote-condition']['answer'] and '99' in cases['threshold-condition']['answer']
    assert '<img' in cases['source-injection']['questions'][0]
    assert cases['human-override']['expectation']['human_category'] == '售后手册'
    assert len(cases['long-catalog']['catalog']) >= 200
    assert len(cases['over-budget']['answer']) > 10000
    assert all(c['questions'] and c['answer'] and c['expectation']['reason'] for c in cases.values())


def test_interrupted_category_evaluation_keeps_each_actual_result_pending():
    from retailq.config import Settings
    from retailq.knowledge.review_contracts import CategorySuggestion
    module=evaluator()
    snapshots=[]
    class Gateway:
        calls=0
        def classify(self,candidates):
            self.calls+=1
            if self.calls==2: raise KeyboardInterrupt()
            return [CategorySuggestion(group_id=c.group_id,category='配送规则',reason='第一例实际结果') for c in candidates]
    with pytest.raises(KeyboardInterrupt):
        module.evaluate_cases(module.load_cases(module.DEFAULT_CASES),Settings(_env_file=None,context_window_tokens=31536),
            Gateway(),checkpoint=lambda report:snapshots.append(copy.deepcopy(report)))
    assert len(snapshots)>=2
    assert snapshots[0]['results']==[] and snapshots[0]['run_completed'] is False
    assert len(snapshots[-1]['results'])==1
    assert snapshots[-1]['results'][0]['suggestion']['reason']=='第一例实际结果'
    assert module.summarize_category_report(snapshots[-1])['status']=='pending'


def test_cli_interrupt_preserves_partial_original_report_and_returns_pending(monkeypatch):
    from pathlib import Path
    import uuid
    from retailq.config import Settings
    from retailq.knowledge.review_contracts import CategorySuggestion
    module=evaluator()
    settings=Settings(_env_file=None,openai_base_url='http://fixture.invalid/v1',openai_api_key='fixture',
        openai_model='fixture',context_window_tokens=31536)
    monkeypatch.setattr(module,'Settings',lambda:settings)
    class Gateway:
        calls=0
        def __init__(self,settings): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def classify(self,candidates):
            self.calls+=1
            if self.calls==2: raise KeyboardInterrupt()
            return [CategorySuggestion(group_id=c.group_id,category='配送规则',reason='保留的实际第一例输出') for c in candidates]
    monkeypatch.setattr(module,'OpenAIQAGateway',Gateway)
    output=Path('artifacts/verification/ch03-review')/('category-interrupt-test-'+uuid.uuid4().hex)
    try:
        result=module.main(['--output',str(output)])
    except KeyboardInterrupt:
        pytest.fail('CLI interruption escaped before preserving partial evidence')
    assert result==2
    report=json.loads((output/'report.json').read_text(encoding='utf-8'))
    assert report['run_completed'] is False and len(report['results'])==1
    assert report['results'][0]['suggestion']['reason']=='保留的实际第一例输出'
    assert module.summarize_category_report(report)['status']=='pending'


def test_collector_refuses_remote_or_confusing_urls_before_requests():
    assert importlib.util.find_spec('scripts.smoke_knowledge_console'), 'console evidence collector is missing'
    from scripts.smoke_knowledge_console import validate_base_url
    for url in ('https://example.com','http://127.0.0.1.evil.test','http://user@localhost:8000',
                'http://localhost:8000/path','http://localhost:8000?redirect=x','file:///knowledge'):
        with pytest.raises(ValueError): validate_base_url(url)
    assert validate_base_url('http://127.0.0.1:8000') == 'http://127.0.0.1:8000'


def test_collector_only_gets_existing_projections_and_preserves_audit(api_env):
    from tests.test_review_api import prepare
    assert importlib.util.find_spec('scripts.smoke_knowledge_console'), 'console evidence collector is missing'
    from scripts.smoke_knowledge_console import collect_console
    rid, group = prepare(api_env)
    before = {p.relative_to(api_env.root):p.read_bytes() for p in api_env.root.rglob('*.json')}
    methods = []
    class Client:
        def get(self, path):
            methods.append('GET')
            return api_env.client.get(path)
    report = collect_console('http://127.0.0.1:8000', rid, client=Client())
    assert report['evidence'] == 'captured' and len(report['groups']) == 1
    assert report['groups'][0]['target_confirmed'] is False
    assert report['operations'][0]['kind'] == 'prepare'
    assert methods and set(methods) == {'GET'}
    assert before == {p.relative_to(api_env.root):p.read_bytes() for p in api_env.root.rglob('*.json')}


from tests.test_review_api import api_env
