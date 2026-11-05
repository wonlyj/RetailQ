"""Single-turn query normalization with conservative literal safeguards."""
import re
import time

from retailq.errors import ServiceError
from retailq.knowledge.retrieval_contracts import QueryUnderstanding


_CONSTRAINTS = re.compile(r'\d+[A-Za-z]+[A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|[A-Za-z]+[A-Za-z0-9]*\d[A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|[A-Za-z]+(?:[-_][A-Za-z0-9]+)+|\d+(?:\.\d+)?(?:摄氏度|华氏度|千克|公斤|毫升|厘米|毫米|公里|小时|分钟|个月|天|日|元|年|次|件|个|米|升|克|℃|℉|%|[\u4e00-\u9fff]{1,2})?|(?:不能|不可|没有|不支持|不可以|不|无|未|签收后|付款后|下单后|发货后|购买后|收到后)')


def _contains_constraint(text,token):
    if re.search(r'[A-Za-z]',token):
        return re.search(r'(?<![A-Za-z0-9_-])'+re.escape(token)+r'(?![A-Za-z0-9_-])',text) is not None
    if token[0].isdigit():
        return re.search(r'(?<![\d.])'+re.escape(token)+r'(?![\d.])',text) is not None
    return token in text


class QueryNormalizer:
    def __init__(self, normalize, synonyms, *, max_expansions=5):
        self.normalize, self.synonyms, self.max_expansions = normalize, synonyms, max_expansions

    def understand(self, raw_question: str, *, deadline: float) -> QueryUnderstanding:
        if not isinstance(raw_question, str) or not raw_question.strip():
            raise ValueError('empty question')
        if time.monotonic() >= deadline:
            raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
        reason = None
        try:
            proposed = self.normalize(raw_question, deadline)
        except ServiceError as exc:
            if exc.code != 'knowledge_query_understanding_failed': raise
            proposed, reason = raw_question, 'normalization_failed'
        if time.monotonic() >= deadline:
            raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
        if not isinstance(proposed, str) or not proposed.strip():
            proposed, reason = raw_question, 'empty_normalization'
        elif any(not _contains_constraint(proposed,token) for token in _CONSTRAINTS.findall(raw_question)):
            proposed, reason = raw_question, 'literal_constraint_lost'
        search_text = proposed if proposed == raw_question else proposed + ' ' + raw_question
        terms = []
        for term, expansions in self.synonyms.items():
            if term in search_text:
                for expansion in expansions:
                    if expansion not in search_text and expansion not in terms:
                        terms.append(expansion)
        bm25 = search_text + ((' ' + ' '.join(terms[:self.max_expansions])) if terms else '')
        return QueryUnderstanding(raw_question, proposed, bm25, reason)
