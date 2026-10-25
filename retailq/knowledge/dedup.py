"""Whole-run candidates; only explicit direct LLM equivalence can merge."""
import threading

import numpy as np

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft,DedupCandidate,DedupGroup
from retailq.knowledge.text import embedding_text,normalize_text


def validate_groups(groups,candidate):
    rows={r.id:r for r in candidate.staging}
    existing={r.id for r in candidate.existing}
    seen=[]
    for group in groups:
        if any(type(i) is not int or i not in rows for i in group.member_ids):raise ValueError('unknown staging id')
        if group.target_chunk_id is not None and group.target_chunk_id not in existing:raise ValueError('unknown existing id')
        if group.representative_id is not None and group.representative_id!=min(group.member_ids):raise ValueError('invalid representative')
        expected={rows[i].question for i in group.member_ids}
        if set(group.questions)!=expected or len(set(group.questions))!=len(group.questions):raise ValueError('questions must be source questions')
        seen.extend(group.member_ids)
    if len(seen)!=len(set(seen)) or set(seen)!=set(rows):raise ValueError('incomplete or duplicate assignment')


def global_dedup(staging,existing,embedding,gateway,*,min_score=0.85,candidate_k=8,stop: threading.Event | None=None):
    active=sorted((r for r in staging if r.status=='extracted'),key=lambda r:r.id)
    if not active:return []
    exact={}
    for row in active:exact.setdefault((normalize_text(row.question),normalize_text(row.answer)),[]).append(row)
    buckets={group[0].id:group for group in exact.values()}
    representatives=[group[0] for group in exact.values()]
    drafts=[ChunkDraft('对话知识',r.question,r.answer,None,'faq') for r in representatives]
    all_vectors=np.asarray(embedding.embed([embedding_text(d) for d in drafts]+[embedding_text(r.draft) for r in existing]),dtype=np.float64)
    if len(all_vectors)!=len(representatives)+len(existing) or not np.isfinite(all_vectors).all():raise ValueError('invalid dedup vectors')
    norms=np.linalg.norm(all_vectors,axis=1)
    if (norms<=0).any():raise ValueError('invalid dedup vector norm')
    all_vectors=all_vectors/norms[:,None]
    claimed=set();result=[]
    for index,anchor in enumerate(representatives):
        if stop is not None and stop.is_set():raise ServiceError('knowledge_job_stopped',409,'知识任务已请求停止，保留进度。')
        if anchor.id in claimed:continue
        target=next((r for r in existing if normalize_text(anchor.answer)==normalize_text(r.draft.answer) and normalize_text(anchor.question) in [normalize_text(q) for q in r.draft.questions.splitlines()]),None)
        selected=[anchor]
        if target is None:
            # One score row at a time: never allocate an N x N similarity matrix.
            scores=all_vectors@all_vectors[index]
            options=[]
            for j,row in enumerate(representatives):
                if row.id!=anchor.id and row.id not in claimed and scores[j]>=min_score:options.append((float(scores[j]),'new',row))
            for j,row in enumerate(existing,len(representatives)):
                if scores[j]>=min_score:options.append((float(scores[j]),'existing',row))
            options.sort(key=lambda item:(-item[0],item[1],item[2].id))
            for _,kind,row in options[:candidate_k]:
                if stop is not None and stop.is_set():raise ServiceError('knowledge_job_stopped',409,'知识任务已请求停止，保留进度。')
                candidate=DedupCandidate(anchor.id,(anchor,row) if kind=='new' else (anchor,),(row,) if kind=='existing' else ())
                try:
                    groups=gateway.deduplicate([candidate])
                    validate_groups(groups,candidate)
                except ServiceError as exc:
                    if exc.code=='input_budget_exceeded':continue  # Uncertain: retain, never cut conditions.
                    raise
                except (ValueError,TypeError,KeyError):
                    raise ServiceError('knowledge_dedup_invalid',502,'知识去重引用或问法无效，未执行入库。') from None
                choice=next(g for g in groups if anchor.id in g.member_ids)
                if choice.target_chunk_id is not None:
                    target=next(r for r in existing if r.id==choice.target_chunk_id)
                    # Other new rows require their own direct comparison to this target.
                    selected=[anchor];break
                if kind=='new' and row.id in choice.member_ids:selected.append(row)
        members=sorted((r for chosen in selected for r in buckets[chosen.id]),key=lambda r:r.id)
        claimed.update(r.id for r in selected)
        result.append(DedupGroup(tuple(r.id for r in members),None if target else members[0].id,target.id if target else None,tuple(dict.fromkeys(r.question for r in members))))
    if sorted(i for group in result for i in group.member_ids)!=[r.id for r in active]:raise ValueError('global dedup coverage mismatch')
    return result
