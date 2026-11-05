import json
from pathlib import Path

import pytest


def test_reviewed_cases_have_disjoint_families_and_expected_coverage():
    from scripts.evaluate_relevance import read_relevance_cases
    cases=read_relevance_cases(Path('evals/ch04_relevance_cases.jsonl'))
    assert {case['kind'] for case in cases if case['split']=='calibration'} >= {'colloquial','missing_related','unrelated','model'}
    assert {case['kind'] for case in cases if case['split']=='test'} >= {'colloquial','missing_related','unrelated','model','multi','forbidden'}
    assert sum(case['kind']=='colloquial' and case['split']=='test' for case in cases)>=5


def test_relevance_cases_reject_family_leakage_and_missing_ground_truth(tmp_path):
    from scripts.evaluate_relevance import read_relevance_cases
    path=tmp_path/'cases.jsonl'
    rows=[{'id':'K01','split':'calibration','family':'postage','kind':'colloquial','query':'x',
           'expected':'y','source':'z','answerable':True},
          {'id':'H01','split':'test','family':'postage','kind':'colloquial','query':'a',
           'expected':'b','source':'z','answerable':True}]
    path.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
    with pytest.raises(ValueError): read_relevance_cases(path)
    rows[1]['family']='other';rows[1]['expected']=''
    path.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
    with pytest.raises(ValueError): read_relevance_cases(path)


def test_relevance_collector_rejects_nonisolated_data_directory_and_prefix():
    from retailq.config import Settings
    from scripts.evaluate_relevance import validate_isolation
    kwargs={'mysql_host':'127.0.0.1','mysql_port':3308,'mysql_database':'retailq_test',
            'milvus_uri':'http://127.0.0.1:19531','milvus_collection_prefix':'eval_ch04',
            'knowledge_data_dir':'artifacts/ch04-eval-knowledge'}
    validate_isolation(Settings(_env_file=None,**kwargs))
    for key,value in [('knowledge_data_dir','artifacts/knowledge'),('milvus_collection_prefix','knowledge')]:
        with pytest.raises(ValueError): validate_isolation(Settings(_env_file=None,**(kwargs|{key:value})))


def test_evidence_sources_resolve_and_multi_case_really_crosses_sections(tmp_path):
    from scripts.evaluate_relevance import read_relevance_cases, validate_case_sources
    cases=read_relevance_cases(Path('evals/ch04_relevance_cases.jsonl'))
    digests=validate_case_sources(cases,Path('examples/knowledge'))
    assert 'product-faq.md' in digests and 'after-sales-manual.md' in digests
    multi=next(case for case in cases if case['kind']=='multi')
    assert len(multi['source'].split(' + '))>=2
    invalid=[dict(cases[0],source='product-faq.md / 不存在的章节')]
    with pytest.raises(ValueError): validate_case_sources(invalid,Path('examples/knowledge'))


def test_evidence_source_rejects_cross_branch_section_path(tmp_path):
    from scripts.evaluate_relevance import validate_case_sources
    (tmp_path/'example.md').write_text('# A\n## B\n# C\n## D\n',encoding='utf-8')
    with pytest.raises(ValueError):
        validate_case_sources([{'source':'example.md / A / D','answerable':True}],tmp_path)


def test_evidence_source_digest_matches_normalized_corpus_identity(tmp_path):
    from hashlib import sha256
    from scripts.evaluate_relevance import validate_case_sources
    (tmp_path/'example.md').write_bytes(b'\xef\xbb\xbf# Section\r\ncontent  \r\n')
    result=validate_case_sources([{'source':'example.md / Section','answerable':True}],tmp_path)
    assert result['example.md']==sha256(b'# Section\ncontent').hexdigest()
