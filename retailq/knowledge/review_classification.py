"""Budgeted category suggestions. Never cut a source answer or approve a group."""
import json

from retailq.context import estimate_tokens
from retailq.errors import ServiceError
from retailq.knowledge.qa_gateway import ClassificationResult, messages_for, prompt
from retailq.knowledge.review_contracts import CategoryCandidate, CategorySuggestion, evolve


def classification_cost(candidates):
    messages = messages_for(ClassificationResult, prompt('qa_category.txt'),
                            [c.model_dump(mode='json') for c in candidates])
    return estimate_tokens(messages) + len(json.dumps(
        ClassificationResult.model_json_schema(), ensure_ascii=False).encode('utf-8')) + 64


def check_stop(stop):
    if stop.is_set():
        raise ServiceError('knowledge_job_stopped', 409, '知识任务已请求停止，保留进度。')


def classify_groups(groups, catalog, gateway, *, input_budget, stop):
    results, batch = {}, []

    def flush():
        if not batch:
            return
        check_stop(stop)
        output = gateway.classify(batch)
        ids = [item.group_id for item in output]
        if len(ids) != len(set(ids)) or set(ids) != {c.group_id for c in batch}:
            raise ServiceError('knowledge_category_invalid', 502, '分类建议未完整匹配审核组，请重试。')
        by_id = {c.group_id: c for c in batch}
        for suggestion in output:
            # Validate doubles and gateway implementations at this boundary too.
            suggestion = CategorySuggestion.model_validate_json(suggestion.model_dump_json())
            if by_id[suggestion.group_id].catalog_truncated:
                prefix = '分类目录未完整提供；'
                suggestion = evolve(suggestion, reason=prefix + suggestion.reason[:300 - len(prefix)])
            results[suggestion.group_id] = suggestion
        batch.clear()

    for group in groups:
        check_stop(stop)
        categories = tuple(catalog)
        candidate = CategoryCandidate(group_id=group.group_id, questions=group.questions,
                                       answer=group.answer, categories=categories, catalog_truncated=False)
        # Remove whole catalog entries only. Questions and answer stay complete.
        while candidate.categories and classification_cost([candidate]) > input_budget:
            candidate = evolve(candidate, categories=candidate.categories[:-1], catalog_truncated=True)
        if classification_cost([candidate]) > input_budget:
            results[group.group_id] = CategorySuggestion(group_id=group.group_id, category=None,
                reason='完整问答与分类结构超过模型预算，请人工选择分类。')
            continue
        if batch and classification_cost(batch + [candidate]) > input_budget:
            flush()
        batch.append(candidate)
    flush()
    return tuple(results[g.group_id] for g in groups)
