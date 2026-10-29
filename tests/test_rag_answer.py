from types import SimpleNamespace

import pytest

from retailq.errors import ServiceError
from retailq.knowledge.answer import ConfidencePolicy, RAGDecision, prepare_bundle, validate_decision
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.retrieval_contracts import EvidenceChunk, QueryUnderstanding


def retrieval(n):
    draft = ChunkDraft('配送', '规则是什么？', '演示规则：未满99元收取10元。', None, 'policy')
    candidates = tuple(EvidenceChunk(i, draft, i, 1/i) for i in range(1, n+1))
    return SimpleNamespace(candidates=candidates, understanding=QueryUnderstanding('用户问题', '用户问题', '用户问题'),
                           snapshot=SimpleNamespace(sources=(), corpus_hash='abc', approved_ids=tuple(range(1,n+1))), strategy='hybrid_rerank')


@pytest.mark.parametrize(('count', 'ranks'), [(1,[1]), (2,[1,2]), (3,[1,3,2]),
    (10,[1,3,5,7,9,10,8,6,4,2])])
def test_bundle_places_strongest_chunks_at_edges(count, ranks):
    bundle = prepare_bundle(retrieval(count), system_prompt='系统约束', input_budget=50000)
    assert [c.relevance_rank for c in bundle.citations] == ranks
    assert [c.number for c in bundle.citations] == list(range(1, count+1))


def test_answer_citations_must_match_bundle():
    bundle = prepare_bundle(retrieval(2), system_prompt='系统约束', input_budget=50000)
    validate_decision(RAGDecision(useful=True, answer='规则是10元[1]。', reason='', citations=[1]), bundle)
    for answer, citations in [('无引用', []), ('错误[3]', [3]), ('不一致[1]', [2])]:
        with pytest.raises(ServiceError):
            validate_decision(RAGDecision(useful=True, answer=answer, reason='', citations=citations), bundle)


def test_insufficient_input_budget_rejects_complete_chunk():
    with pytest.raises(ServiceError):
        prepare_bundle(retrieval(1), system_prompt='系统约束', input_budget=3)


def test_budget_removes_lowest_relevance_and_renumbers():
    from langchain_core.messages import HumanMessage, SystemMessage
    from retailq.context import estimate_tokens
    import json
    one = prepare_bundle(retrieval(1), system_prompt='系统约束', input_budget=50000)
    budget = estimate_tokens([SystemMessage(content=one.messages[0][1]), HumanMessage(content=one.messages[1][1])])
    budget += len(json.dumps(RAGDecision.model_json_schema(), ensure_ascii=False).encode('utf-8')) + 64
    result = prepare_bundle(retrieval(3), system_prompt='系统约束', input_budget=budget)
    assert [item.relevance_rank for item in result.citations] == [1]
    assert [item.number for item in result.citations] == [1]


def test_source_snapshot_preserves_origin_locator():
    result = retrieval(1)
    result.snapshot.sources = (SimpleNamespace(chunk_ids=(1,), line_ranges=((1, 10, 15),),
                                               key='policy.md', digest='f'*64, archive='sources/policy.md'),)
    result.snapshot.approved_ids = ()
    source = prepare_bundle(result, system_prompt='系统约束', input_budget=50000).citations[0]
    assert (source.source_key, source.source_version, source.line_start, source.line_end) == ('policy.md', 'f'*64, 10, 15)
    result.snapshot.sources[0].line_ranges = ()
    with pytest.raises(ServiceError):
        prepare_bundle(result, system_prompt='系统约束', input_budget=50000)


def test_confidence_policy_rejects_uncalibrated_or_identity_drift(tmp_path):
    import json
    path = tmp_path / 'policy.json'
    identity = {'model':'m', 'corpus_hash':'abc', 'prompt_hash':'prompt', 'query_hash':'query', 'strategy':'hybrid_rerank'}
    path.write_text(json.dumps({'identity': identity, 'calibrated': False, 'thresholds': {}}), encoding='utf-8')
    with pytest.raises(ServiceError):
        ConfidencePolicy.load(path, expected_identity=identity)
    path.write_text(json.dumps({'identity': identity, 'calibrated': True, 'thresholds': {'hybrid_rerank': .5}}), encoding='utf-8')
    with pytest.raises(ServiceError):
        ConfidencePolicy.load(path, expected_identity={'model':'other'})
    policy = ConfidencePolicy.load(path, expected_identity=identity)
    assert policy.allows(retrieval(1))
    assert not policy.allows(retrieval(0))
