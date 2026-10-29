import json
import time
import httpx
import pytest

from retailq.errors import ServiceError
from retailq.knowledge.rerank import Reranker
from retailq.knowledge.retrieval_contracts import EvidenceChunk
from retailq.knowledge.contracts import ChunkDraft


def candidate(item_id):
    return EvidenceChunk(item_id, ChunkDraft('分类', '问题', '答案', None, 'faq'), 1, 0.5)


@pytest.mark.parametrize('results', [
    [{'index': 0, 'relevance_score': 0.9}, {'index': 0, 'relevance_score': 0.8}],
    [{'index': 3, 'relevance_score': 0.9}],
    [{'index': 0, 'relevance_score': float('nan')}],
])
def test_rerank_rejects_bad_index_or_score(results):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'results': results}))
    reranker = Reranker('https://example.test/v1', 'secret', transport=transport)
    with pytest.raises(ServiceError):
        reranker.rerank('原问题', (candidate(1), candidate(2)), deadline=time.monotonic()+10)
    reranker.close()


def test_rerank_maps_original_index_and_uses_fixed_model():
    def respond(request):
        data = json.loads(request.content)
        assert data['model'] == 'BAAI/bge-reranker-v2-m3'
        assert data['query'] == '原问题' and data['top_n'] == 10 and data['return_documents'] is False
        return httpx.Response(200, json={'results': [{'index': 1, 'relevance_score': .9}, {'index': 0, 'relevance_score': .1}]})
    reranker = Reranker('https://example.test/v1', 'secret', transport=httpx.MockTransport(respond))
    result = reranker.rerank('原问题', (candidate(1), candidate(2)), deadline=time.monotonic()+10)
    assert [item.id for item in result] == [2, 1]
    reranker.close()
