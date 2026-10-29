"""SiliconFlow BGE reranker adapter with index and score validation."""
from dataclasses import replace
import math
import time
import httpx

from retailq.errors import ServiceError
from retailq.knowledge.text import embedding_text


class Reranker:
    def __init__(self, base_url, api_key, *, proxy=None, timeout_seconds=20, transport=None):
        if not api_key:
            raise ServiceError('rerank_not_configured', 503, '请配置重排服务。')
        self.url = base_url.rstrip('/') + '/rerank'
        self.timeout = timeout_seconds
        self.client = httpx.Client(transport=transport, proxy=proxy or None, trust_env=False,
                                   headers={'Authorization': 'Bearer ' + api_key})

    def close(self):
        self.client.close()

    def rerank(self, query, candidates, *, deadline):
        if not candidates:
            return ()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
        try:
            response = self.client.post(self.url, json={'model': 'BAAI/bge-reranker-v2-m3', 'query': query,
                'documents': [embedding_text(item.draft) for item in candidates], 'top_n': 10,
                'return_documents': False}, timeout=min(self.timeout, remaining))
            response.raise_for_status()
            if time.monotonic() >= deadline:
                raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
            results = response.json()['results']
            if not isinstance(results, list) or len(results) != min(10, len(candidates)):
                raise ValueError('incorrect result count')
            seen, output = set(), []
            for rank, row in enumerate(results, 1):
                index, score = row['index'], row['relevance_score']
                if type(index) is not int or not 0 <= index < len(candidates) or index in seen:
                    raise ValueError('invalid index')
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError('invalid score')
                seen.add(index)
                output.append(replace(candidates[index], rank=rank, score=float(score)))
            return tuple(output)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ServiceError('rerank_unavailable', 503, '重排服务未成功，请稍后重试。') from None
