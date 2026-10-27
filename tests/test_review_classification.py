"""Catch invalid model assignments and silent truncation of classification inputs."""
import json
from threading import Event

import httpx
import pytest

from retailq.config import Settings
from retailq.errors import ServiceError
from tests.support.reviews import RID, ReviewGateway


def candidate():
    from retailq.knowledge.review_contracts import CategoryCandidate
    return CategoryCandidate(group_id=RID, questions=('怎么清洁？',), answer='擦干后阴凉晾干。',
                              categories=('售后手册',), catalog_truncated=False)


def model(content, reason='stop', observe=None):
    from retailq.knowledge.qa_gateway import OpenAIQAGateway
    def respond(request):
        body = json.loads(request.content)
        assert body['response_format']['type'] == 'json_object' and not body.get('tools')
        if observe:
            observe(body)
        return httpx.Response(200, json={'id':'test','object':'chat.completion','created':0,'model':'test-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':reason}]})
    settings = Settings(_env_file=None, openai_base_url='http://qa.test/v1', openai_api_key='test',
                        openai_model='test-model', context_window_tokens=50000)
    return OpenAIQAGateway(settings, transport=httpx.MockTransport(respond))


def suggestion(**kwargs):
    return {'group_id':RID, 'category':'售后手册', 'reason':'已有目录适用，需人工确认。',
            'recommend_reject':False, **kwargs}


def test_real_langchain_classification_keeps_complete_qa_and_schema():
    seen = []
    with model(json.dumps({'items':[suggestion()]}, ensure_ascii=False), observe=seen.append) as gateway:
        result = gateway.classify([candidate()])
    assert result[0].category == '售后手册'
    data = json.loads(seen[0]['messages'][-1]['content'])
    assert data[0]['answer'] == '擦干后阴凉晾干。' and data[0]['group_id'] == RID


@pytest.mark.parametrize('items', [
    [suggestion(group_id='b'*32)], [suggestion(), suggestion()], [],
    [suggestion(decision='approved')], [suggestion(answer='改写答案')],
    [suggestion(category='\x00类')],
])
def test_duplicate_unknown_or_extra_model_fields_are_rejected(items):
    with model(json.dumps({'items':items}, ensure_ascii=False)) as gateway:
        with pytest.raises(ServiceError):
            gateway.classify([candidate()])


@pytest.mark.parametrize('content,reason', [(' {"items": [', 'stop'), ('{"items":[]}', 'length')])
def test_incomplete_json_is_not_a_category_suggestion(content, reason):
    with model(content, reason) as gateway:
        with pytest.raises(ServiceError):
            gateway.classify([candidate()])


def test_null_category_remains_pending_human_choice():
    with model(json.dumps({'items':[suggestion(category=None, recommend_reject=True)]})) as gateway:
        result = gateway.classify([candidate()])
    assert result[0].category is None and result[0].recommend_reject is True


def test_oversize_group_is_not_cut_or_sent_to_model():
    from retailq.knowledge.review_classification import classify_groups
    from retailq.knowledge.review_contracts import ReviewGroup, SourceQA
    member = SourceQA(staging_id=1, batch_no='c'*64, source_ref='source', question='问题',
                      answer='完整条件。'*1000, message_ids=(1,2))
    group = ReviewGroup(run_id=RID, group_id='b'*32, members=(member,), representative_id=1,
                        questions=('问题',), answer=member.answer)
    gateway = ReviewGateway()
    results = classify_groups((group,), (), gateway, input_budget=1000, stop=Event())
    assert results[0].category is None and '预算' in results[0].reason
    assert gateway.classified_ids == []


def test_catalog_budget_is_explicit_and_whole_answer_preserved():
    from retailq.knowledge.review_classification import classify_groups, classification_cost
    from retailq.knowledge.review_contracts import ReviewGroup, SourceQA, CategoryCandidate
    member = SourceQA(staging_id=1, batch_no='c'*64, source_ref='s', question='问', answer='完整答案。', message_ids=(1,2))
    group = ReviewGroup(run_id=RID, group_id='b'*32, members=(member,), representative_id=1,
                        questions=('问',), answer=member.answer)
    empty = CategoryCandidate(group_id='b'*32, questions=('问',), answer='完整答案。', categories=(), catalog_truncated=True)
    budget = classification_cost([empty]) + 20
    seen = []
    class Observed(ReviewGateway):
        def classify(self, candidates):
            seen.extend(candidates)
            return super().classify(candidates)
    result = classify_groups((group,), tuple('类'+str(i)+'x'*100 for i in range(100)), Observed(), input_budget=budget, stop=Event())
    assert seen[0].catalog_truncated and seen[0].answer == '完整答案。'
    assert '目录' in result[0].reason
