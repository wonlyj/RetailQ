from types import SimpleNamespace
import pytest

from retailq.knowledge.evaluation.judge import validate_judgement


def test_judge_rejects_references_outside_evidence():
    with pytest.raises(ValueError):
        validate_judgement({'claims':[{'text':'x','supported':True,'citations':[2],'reason':''}]},
                           'x [1]', [SimpleNamespace(number=1)])


def test_judge_rejects_claim_not_present_in_answer():
    with pytest.raises(ValueError):
        validate_judgement({'claims':[{'text':'fabricated','supported':True,'citations':[1],'reason':''}]},
                           'actual [1]', [SimpleNamespace(number=1)])


def test_judge_cannot_claim_unsupported_without_reason():
    with pytest.raises(ValueError):
        validate_judgement({'claims':[{'text':'actual','supported':False,'citations':[],'reason':''}]},
                           'actual [1]', [SimpleNamespace(number=1)])


@pytest.mark.anyio
async def test_judge_receives_only_answer_and_actual_evidence():
    from retailq.knowledge.evaluation.judge import FaithfulnessJudge

    seen=[]
    async def invoke(messages):
        seen.extend(messages)
        return {'claims':[{'text':'运费10元','supported':True,'citations':[1],'reason':''}]}
    judge=FaithfulnessJudge(invoke=invoke)
    result=await judge.judge('运费10元。[1]', [SimpleNamespace(number=1,text='未满99元收10元',questions='运费？')])
    assert result.score == 1
    assert '运费10元' in str(seen) and '未满99元收10元' in str(seen)
    assert all(word not in str(seen) for word in ('ground_truth','strategy','useful'))


@pytest.mark.anyio
async def test_judge_invalid_output_or_timeout_never_becomes_a_score():
    from retailq.knowledge.evaluation.judge import FaithfulnessJudge
    async def malformed(messages): return {'claims':'not-a-list'}
    async def timeout(messages): raise TimeoutError('judge timeout')
    evidence=[SimpleNamespace(number=1,text='source',questions='question')]
    with pytest.raises(ValueError): await FaithfulnessJudge(invoke=malformed).judge('answer [1]',evidence)
    with pytest.raises(TimeoutError): await FaithfulnessJudge(invoke=timeout).judge('answer [1]',evidence)


@pytest.mark.anyio
async def test_runner_uses_one_understanding_and_same_snapshot_for_four_strategies(tmp_path):
    from retailq.knowledge.evaluation.contracts import read_cases, MappedCase
    from retailq.knowledge.evaluation.runner import EvaluationRunner
    from retailq.knowledge.evaluation.store import EvaluationStore
    from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RawHit

    case=next(c for c in read_cases(__import__('pathlib').Path('evals/ch04_cases.jsonl')) if c.eval_id=='B05')
    snapshot=SimpleNamespace(corpus_hash='frozen')
    calls=[]; understand=[]
    def normalize(question, *, deadline):
        understand.append(question)
        return QueryUnderstanding(question,question,question)
    def retrieve(query, snap, strategy, filters, *, deadline):
        calls.append((query,snap,strategy,filters))
        candidate=SimpleNamespace(id=7,rank=1,score=.9)
        return SimpleNamespace(candidates=(candidate,),candidate_hits=(RawHit(7,.9,'f'),),snapshot=snap,strategy=strategy)
    async def answer(bundle):
        return SimpleNamespace(useful=False,answer='',reason='insufficient',citations=[])
    class Gateway: pass
    gateway=Gateway();gateway.answer=answer
    bundle=lambda result: SimpleNamespace(citations=(SimpleNamespace(number=1,chunk_id=7),),bundle_hash='hash')
    store=EvaluationStore.open_new(tmp_path/'run', {'corpus':'frozen'})
    runner=EvaluationRunner(snapshot=snapshot,normalize=normalize,retrieve=retrieve,gateway=gateway,
                            judge=None,bundle_factory=bundle,store=store)
    await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert len(understand)==1 and len(calls)==4
    assert all(call[1] is snapshot and call[3]==calls[0][3] and call[0]==calls[0][0] for call in calls)
    assert {call[2] for call in calls}=={'dense','bm25','hybrid','hybrid_rerank'}
    assert len(store.rows())==4
    assert all(row['faithfulness'] is None and row['phase']=='complete' for row in store.rows())
    assert all(row['evidence_coverage']==1 for row in store.rows())


@pytest.mark.anyio
async def test_runner_resumes_after_generation_error_without_repeating_retrieval(tmp_path):
    from pathlib import Path
    from retailq.knowledge.contracts import ChunkDraft
    from retailq.knowledge.evaluation.contracts import read_cases, MappedCase
    from retailq.knowledge.evaluation.runner import EvaluationRunner
    from retailq.knowledge.evaluation.store import EvaluationStore
    from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RawHit, EvidenceChunk, RetrievalResult

    case=next(c for c in read_cases(Path('evals/ch04_cases.jsonl')) if c.eval_id=='B05')
    snapshot=SimpleNamespace(corpus_hash='frozen')
    calls=[]
    def normalize(question, *, deadline): return QueryUnderstanding(question,question,question)
    def retrieve(query, snap, strategy, filters, *, deadline):
        calls.append(strategy)
        candidate=EvidenceChunk(7,ChunkDraft('c','q','a',None,'faq'),1,.9)
        return RetrievalResult(query,snap,strategy,(candidate,),(RawHit(7,.9,'f'),))
    class Gateway:
        fail=True
        async def answer(self,bundle):
            if self.fail:
                self.fail=False
                raise RuntimeError('transport error')
            return SimpleNamespace(useful=False,answer='',reason='insufficient',citations=[])
    gateway=Gateway()
    bundle=lambda result: SimpleNamespace(citations=(SimpleNamespace(number=1,chunk_id=7),),bundle_hash='hash')
    store=EvaluationStore.open_new(tmp_path/'run',{'corpus':'frozen'})
    runner=EvaluationRunner(snapshot=snapshot,normalize=normalize,retrieve=retrieve,gateway=gateway,
                            judge=None,bundle_factory=bundle,store=store)
    with pytest.raises(RuntimeError): await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert calls==['dense'] and store.rows()[0]['phase']=='retrieval'
    await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert calls==['dense','bm25','hybrid','hybrid_rerank']
    assert store.rows()[0]['latency_incomplete'] is True


@pytest.mark.anyio
async def test_runner_resumes_judge_failure_without_regenerating_answer(tmp_path):
    from pathlib import Path
    from retailq.knowledge.answer import CitationSnapshot, EvidenceBundle, RAGDecision
    from retailq.knowledge.contracts import ChunkDraft
    from retailq.knowledge.evaluation.contracts import read_cases, MappedCase
    from retailq.knowledge.evaluation.judge import JudgeResult
    from retailq.knowledge.evaluation.runner import EvaluationRunner
    from retailq.knowledge.evaluation.store import EvaluationStore
    from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RawHit, EvidenceChunk, RetrievalResult
    case=next(c for c in read_cases(Path('evals/ch04_cases.jsonl')) if c.eval_id=='B05')
    snapshot=SimpleNamespace(corpus_hash='frozen'); calls=[]
    def normalize(question, *, deadline): return QueryUnderstanding(question,question,question)
    def retrieve(query,snap,strategy,filters,*,deadline):
        return RetrievalResult(query,snap,strategy,(EvidenceChunk(7,ChunkDraft('c','q','a',None,'faq'),1,.9),),(RawHit(7,.9,'f'),))
    class Gateway:
        async def answer(self,bundle):
            calls.append('generate');return RAGDecision(useful=True,answer='a [1]',reason='',citations=[1])
    class Judge:
        fail=True
        async def judge(self,answer,citations):
            if self.fail: self.fail=False;raise RuntimeError('judge unavailable')
            return JudgeResult(({'text':'a','supported':True,'citations':[1],'reason':''},),1.0)
    citation=CitationSnapshot(1,7,1,'c','section','q','a','key','v','archive',1,2)
    bundle=lambda result: EvidenceBundle(case.query,result.strategy,'frozen',(citation,),(), 'hash')
    store=EvaluationStore.open_new(tmp_path/'run',{'corpus':'frozen'})
    runner=EvaluationRunner(snapshot=snapshot,normalize=normalize,retrieve=retrieve,gateway=Gateway(),judge=Judge(),bundle_factory=bundle,store=store)
    with pytest.raises(RuntimeError): await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert calls==['generate'] and store.rows()[0]['phase']=='generation'
    await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert len(calls)==4 and all(row['phase']=='complete' for row in store.rows())


@pytest.mark.anyio
async def test_sql_receipt_can_be_replayed_after_checkpoint_failure(tmp_path,monkeypatch):
    from pathlib import Path
    from retailq.knowledge.answer import CitationSnapshot, EvidenceBundle, RAGDecision
    from retailq.knowledge.contracts import ChunkDraft
    from retailq.knowledge.evaluation.contracts import read_cases, MappedCase
    from retailq.knowledge.evaluation.judge import JudgeResult
    from retailq.knowledge.evaluation.runner import EvaluationRunner
    from retailq.knowledge.evaluation.store import EvaluationStore
    from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RawHit, EvidenceChunk, RetrievalResult
    case=next(c for c in read_cases(Path('evals/ch04_cases.jsonl')) if c.eval_id=='B05')
    snapshot=SimpleNamespace(corpus_hash='frozen'); receipts=set(); calls=[]
    def normalize(question, *, deadline): return QueryUnderstanding(question,question,question)
    def retrieve(query,snap,strategy,filters,*,deadline):
        return RetrievalResult(query,snap,strategy,(EvidenceChunk(7,ChunkDraft('c','q','a',None,'faq'),1,.9),),(RawHit(7,.9,'f'),))
    class Gateway:
        async def answer(self,bundle): return RAGDecision(useful=True,answer='a [1]',reason='',citations=[1])
    class Judge:
        async def judge(self,answer,citations): return JudgeResult(({'text':'a','supported':False,'citations':[],'reason':'unsupported'},),0.0)
    async def ledger(case,decision,bundle,judged): calls.append(case.eval_id);receipts.add(case.eval_id)
    citation=CitationSnapshot(1,7,1,'c','section','q','a','key','v','archive',1,2)
    bundle=lambda result: EvidenceBundle(case.query,result.strategy,'frozen',(citation,),(), 'hash')
    store=EvaluationStore.open_new(tmp_path/'run',{'corpus':'frozen'})
    original=store.checkpoint; failed=False
    def checkpoint(row):
        nonlocal failed
        if row['strategy']=='hybrid_rerank' and row['phase']=='complete' and not failed:
            failed=True;raise OSError('checkpoint interrupted')
        original(row)
    monkeypatch.setattr(store,'checkpoint',checkpoint)
    runner=EvaluationRunner(snapshot=snapshot,normalize=normalize,retrieve=retrieve,gateway=Gateway(),judge=Judge(),bundle_factory=bundle,store=store,ledger=ledger)
    with pytest.raises(OSError): await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert calls==['B05'] and receipts=={'B05'}
    await runner.run([MappedCase(case,(frozenset({7}),))],split='test')
    assert calls==['B05','B05'] and receipts=={'B05'}


@pytest.mark.anyio
async def test_cancelled_blocking_work_waits_for_actual_thread():
    import asyncio
    import threading
    from retailq.knowledge.evaluation.runner import _owned_thread
    entered=threading.Event();release=threading.Event()
    def work():
        entered.set();release.wait(3)
    task=asyncio.create_task(_owned_thread(work))
    assert await asyncio.to_thread(entered.wait,2)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
