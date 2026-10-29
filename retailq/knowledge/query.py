"""Single-turn query normalization with conservative literal safeguards."""
import re
import time

from retailq.errors import ServiceError
from retailq.knowledge.retrieval_contracts import QueryUnderstanding


_CONSTRAINTS = re.compile(r'[A-Za-z]+(?:[-_][A-Za-z0-9]+)+|\d+(?:\.\d+)?(?:小时|天|日|元|分钟|个月|年|次|件|%|毫升|升|公斤|kg|g)?|(?:不能|不可|没有|不支持|不可以|不|无|未|签收后|付款后|下单后|发货后|购买后|收到后)')


class QueryNormalizer:
    def __init__(self, normalize, synonyms, *, max_expansions=5):
        self.normalize, self.synonyms, self.max_expansions = normalize, synonyms, max_expansions

    def understand(self, raw_question: str, *, deadline: float) -> QueryUnderstanding:
        if not isinstance(raw_question, str) or not raw_question.strip():
            raise ValueError('empty question')
        if time.monotonic() >= deadline:
            raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
        proposed = self.normalize(raw_question, deadline)
        if time.monotonic() >= deadline:
            raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
        reason = None
        if not isinstance(proposed, str) or not proposed.strip():
            proposed, reason = raw_question, 'empty_normalization'
        elif any(token not in proposed for token in _CONSTRAINTS.findall(raw_question)):
            proposed, reason = raw_question, 'literal_constraint_lost'
        terms = []
        for term, expansions in self.synonyms.items():
            if term in proposed:
                for expansion in expansions:
                    if expansion not in proposed and expansion not in terms:
                        terms.append(expansion)
        bm25 = proposed + ((' ' + ' '.join(terms[:self.max_expansions])) if terms else '')
        return QueryUnderstanding(raw_question, proposed, bm25, reason)
