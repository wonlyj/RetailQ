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


@pytest.mark.mysql
@pytest.mark.lite
def test_resume_after_group_commit_before_checkpoint(service,tmp_path,monkeypatch):
    from retailq.knowledge.mining import MiningPipeline
    from tests.test_knowledge_extraction import snapshot,proposal
    monkeypatch.setattr(service.repository,'closed_conversations',lambda:[snapshot()])
    class Extractor(Gateway):
        def extract(self,batch):return [proposal()]
    original=service.repository.apply_dedup
    def failed(groups):
        original(groups)
        raise RuntimeError('crash after committed group')
    monkeypatch.setattr(service.repository,'apply_dedup',failed)
    pipeline=MiningPipeline(service.repository,Extractor(),service.embedding,service,tmp_path/'runs')
    first=pipeline.run()
    assert not first.complete
    count=len(service.repository.all_chunks())
    monkeypatch.setattr(service.repository,'apply_dedup',original)
    assert pipeline.run(resume_run_id=first.run_id).complete
    assert len(service.repository.all_chunks())==count


@pytest.mark.mysql
@pytest.mark.lite
def test_resume_after_each_phase_and_missing_run_manifest(service,tmp_path,monkeypatch):
    from retailq.knowledge.mining import MiningPipeline
    from retailq.knowledge.contracts import ConversationSnapshot,MiningMessage
    snapshots=[ConversationSnapshot(f's{i}',i,2,(MiningMessage(1,'user',r.question),MiningMessage(2,'assistant',r.answer))) for i,r in enumerate(rows(),1)]
    monkeypatch.setattr(service.repository,'closed_conversations',lambda:snapshots)
    class Extractor(Gateway):
        def __init__(self):super().__init__();self.failed=False
        def extract(self,batch):
            if not self.failed:
                self.failed=True
                raise ServiceError('knowledge_qa_invalid',502,'forced extraction failure')
            return [QAProposal(s.source_ref,s.messages[0].content,s.messages[1].content,s.messages[1].content) for s in batch]
    gateway=Extractor()
    pipeline=MiningPipeline(service.repository,gateway,service.embedding,service,tmp_path/'runs')
    first=pipeline.run()
    assert not first.complete
    result=pipeline.run(resume_run_id=first.run_id)
    assert result.complete and result.kept==1 and result.discarded==1
    count=len(service.repository.all_chunks())
    for file in (tmp_path/'runs').glob('*.json'):file.unlink()
    again=pipeline.run()
    assert again.complete and len(service.repository.all_chunks())==count


@pytest.mark.mysql
@pytest.mark.lite
def test_resume_after_dual_write_failure(service,tmp_path,monkeypatch):
    from retailq.knowledge.mining import MiningPipeline
    from tests.test_knowledge_extraction import snapshot,proposal
    monkeypatch.setattr(service.repository,'closed_conversations',lambda:[snapshot()])
    class Extractor(Gateway):
        def extract(self,batch):return [proposal()]
    original=service.vectorize_pending
    def failed(**kwargs):raise RuntimeError('forced vector stage failure')
    monkeypatch.setattr(service,'vectorize_pending',failed)
    pipeline=MiningPipeline(service.repository,Extractor(),service.embedding,service,tmp_path/'runs')
    first=pipeline.run()
    assert not first.complete and service.status()['pending']>0
    monkeypatch.setattr(service,'vectorize_pending',original)
    assert pipeline.run(resume_run_id=first.run_id).complete
    assert service.status()['pending']==0
