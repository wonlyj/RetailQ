import time
import pytest

from retailq.knowledge.query import QueryNormalizer


def test_normalization_preserves_model_and_negation():
    raw = 'MH-LP100 不支持的功能有哪些？'
    normalizer = QueryNormalizer(lambda text, deadline: '型号有哪些功能？', {'邮费': ['运费']})
    result = normalizer.understand(raw, deadline=time.monotonic() + 5)
    assert result.normalized_query == raw
    assert result.fallback_reason


def test_normalization_preserves_time_start_and_expands_only_for_search():
    raw = '签收后48小时内不能退货吗？'
    normalizer = QueryNormalizer(lambda text, deadline: '签收后48小时内不能申请退货吗？', {'退货': ['退回商品']})
    result = normalizer.understand(raw, deadline=time.monotonic() + 5)
    assert result.normalized_query == '签收后48小时内不能申请退货吗？'
    assert '退回商品' in result.bm25_query
    assert '退回商品' not in result.normalized_query


def test_bm25_retains_original_words_when_rewritten():
    result = QueryNormalizer(lambda raw, deadline: '配送费用如何计算？', {'邮费':['运费']}).understand('邮费咋算？', deadline=time.monotonic()+5)
    assert '邮费咋算？' in result.bm25_query and '运费' in result.bm25_query


def test_labeled_query_examples():
    import json
    from pathlib import Path
    synonyms = json.loads(Path('examples/knowledge/synonyms.json').read_text(encoding='utf-8'))
    examples = json.loads(Path('tests/data/ch04-query-samples.json').read_text(encoding='utf-8'))
    assert synonyms['version'] == 'ch04-v1' and len(examples) >= 12
    for example in examples:
        normalizer = QueryNormalizer(lambda raw, deadline: example['normalized'], synonyms['terms'])
        result = normalizer.understand(example['raw'], deadline=time.monotonic() + 5)
        assert result.normalized_query == example['normalized']
        if example['expansion']:
            assert example['expansion'] in result.bm25_query


@pytest.mark.parametrize(('raw', 'changed'), [
    ('签收后48小时可以退吗？', '48小时可以退吗？'),
    ('型号 MH-LP100 不支持什么？', '型号 MH-LP100 支持什么？'),
    ('签收后48小时内可退吗？', '签收后24小时内可退吗？'),
])
def test_constraints_are_not_dropped(raw, changed):
    result = QueryNormalizer(lambda question, deadline: changed, {}).understand(raw, deadline=time.monotonic()+5)
    assert result.normalized_query == raw
    assert result.fallback_reason
