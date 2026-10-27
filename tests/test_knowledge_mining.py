from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from retailq.errors import ServiceError
from retailq.knowledge.contracts import QAProposal,DedupGroup,StagedQA
from tests.support.knowledge import service,FakeEmbedding


def rows(answer='演示：请使用冷水轻柔清洗。'):
    return [StagedQA(1,'batch-a','s1','绒布怎么清洗？',answer,'extracted'),StagedQA(2,'batch-b','s2','绒布清洁有什么要求？',answer,'extracted')]


class Gateway:
    def __init__(self):self.calls=[]
    def deduplicate(self,candidates):
        output=[]
        for c in candidates:
            self.calls.append(c)
            anchor=next(r for r in c.staging if r.id==c.anchor_id)
            target=next((r for r in c.existing if r.draft.answer==anchor.answer),None)
            if target:
                output.append(DedupGroup((anchor.id,),None,target.id,(anchor.question,)))
            elif len({r.answer for r in c.staging})==1:
                output.append(DedupGroup(tuple(r.id for r in c.staging),min(r.id for r in c.staging),None,tuple(dict.fromkeys(r.question for r in c.staging))))
            else:
                output.extend(DedupGroup((r.id,),r.id,None,(r.question,)) for r in c.staging)
        return output


def test_cross_batch_duplicates_are_compared_together():
    from retailq.knowledge.dedup import global_dedup
    gateway=Gateway()
    groups=global_dedup(rows(),[],FakeEmbedding(),gateway)
    assert len(groups)==1 and set(groups[0].member_ids)=={1,2}
    assert set(groups[0].questions)=={r.question for r in rows()}
    assert gateway.calls


def test_similar_different_region_or_threshold_is_not_merged():
    from retailq.knowledge.dedup import global_dedup
    candidates=rows();candidates[1]=StagedQA(2,'batch-b','s2','偏远地区的清洗条件？','演示：偏远地区不适用此规则。','extracted')
    groups=global_dedup(candidates,[],FakeEmbedding(),Gateway())
    assert len(groups)==2


@pytest.mark.mysql
@pytest.mark.lite
def test_equivalent_existing_chunk_discards_staging_without_new_row(service):
    from retailq.knowledge.dedup import global_dedup
    existing=service.repository.all_chunks()[0]
    staged=service.repository.stage('batch',[QAProposal('s','另一个真实问法？',existing.draft.answer,existing.draft.answer)])
    groups=global_dedup(staged,service.repository.all_chunks(),FakeEmbedding(),Gateway())
    before=len(service.repository.all_chunks())
    assert service.repository.apply_dedup(groups)==[existing.id]
    assert len(service.repository.all_chunks())==before
    assert service.repository.staging(['batch'])[0].status=='discarded'


@pytest.mark.mysql
@pytest.mark.lite
def test_group_commit_updates_staging_and_pending_atomically(service):
    staged=service.repository.stage('batch',[QAProposal('s',r.question,r.answer,r.answer) for r in rows()])
    group=DedupGroup(tuple(r.id for r in staged),staged[0].id,None,tuple(r.question for r in staged))
    before=len(service.repository.all_chunks())
    def fail(_):raise ValueError('forced group commit failure')
    event.listen(Session,'before_commit',fail)
    try:
        with pytest.raises(ValueError):service.repository.apply_dedup([group])
    finally:event.remove(Session,'before_commit',fail)
    assert len(service.repository.all_chunks())==before
    assert all(r.status=='extracted' for r in service.repository.staging(['batch']))
    ids=service.repository.apply_dedup([group])
    assert service.repository.apply_dedup([group])==ids
    assert len(service.repository.all_chunks())==before+1
    assert [r.status for r in service.repository.staging(['batch'])]==['kept','discarded']
    row=service.repository.get_chunks(ids)[ids[0]]
    assert row.draft.category=='对话知识' and row.draft.section_path is None
    assert row.prev_chunk_id is None and row.next_chunk_id is None and row.vectorize_status=='pending'


def test_malicious_group_ids_or_answer_rewrite_are_rejected():
    from retailq.knowledge.dedup import global_dedup
    class Bad(Gateway):
        def deduplicate(self,candidates):return [DedupGroup((999,),999,None,('伪造问法',))]
    with pytest.raises(ServiceError):global_dedup(rows(),[],FakeEmbedding(),Bad())


def test_real_gateway_rejects_answer_rewrite_and_accepts_source_groups():
    import json
    from retailq.knowledge.contracts import DedupCandidate
    from tests.test_knowledge_extraction import gateway
    candidate=DedupCandidate(1,tuple(rows()),())
    group={'member_ids':[1,2],'representative_id':1,'target_chunk_id':None,'questions':[r.question for r in rows()]}
    with gateway(json.dumps({'groups':[group]},ensure_ascii=False)) as model:
        assert model.deduplicate([candidate])[0].member_ids==(1,2)
    group['answer']='伪造答案'
    with gateway(json.dumps({'groups':[group]},ensure_ascii=False)) as model:
        with pytest.raises(ServiceError):model.deduplicate([candidate])


def test_equivalence_is_not_inferred_transitively():
    from retailq.knowledge.dedup import global_dedup
    candidates=rows()+[StagedQA(3,'batch-c','s3','绒布能机洗吗？','另一个条件。','extracted')]
    groups=global_dedup(candidates,[],FakeEmbedding(),Gateway())
    assert [set(g.member_ids) for g in groups]==[{1,2},{3}]


# Recovery coverage migrated with the review gate:
# - SQL commit before checkpoint -> test_review_application.py::
#   test_sql_commit_before_audit_confirmation_recovers_same_id
# - extraction/checkpoint resume -> test_review_preparation.py::
#   test_prepare_resume_skips_persisted_batches
# - lost run audit must not trigger automatic publication -> test_review_entrypoints.py::
#   test_missing_published_run_audit_cannot_be_recreated_as_unreviewed
# - dual-write failure -> test_review_application.py::
#   test_vector_failure_does_not_request_approval_again
# Actual process termination evidence is owned by Task 10, not these unit hooks.
