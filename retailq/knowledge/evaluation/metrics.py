from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalMetrics:
    recall: dict[int, float | None]
    mrr: float | None


def retrieval_metrics(ranked_ids, relevant_groups, ks=(1, 3, 5, 10)):
    groups = tuple(frozenset(group) for group in relevant_groups)
    if any(not group for group in groups) or any(k <= 0 for k in ks):
        raise ValueError('invalid evidence groups or cutoffs')
    if not groups:
        return RetrievalMetrics({k: None for k in ks}, None)
    ranks = tuple(ranked_ids)
    recall = {k: sum(bool(group.intersection(ranks[:k])) for group in groups) / len(groups) for k in ks}
    first = next((i for i, item in enumerate(ranks, 1) if any(item in group for group in groups)), None)
    return RetrievalMetrics(recall, 1 / first if first else 0.0)


def faithfulness(claims):
    claims = tuple(claims)
    if any(type(claim) is not bool for claim in claims):
        raise ValueError('claim support must be boolean')
    return sum(claims) / len(claims) if claims else None
