"""Evaluate strategies against a fixed snapshot and persist each stage."""
import asyncio
from dataclasses import asdict, is_dataclass
import time

from retailq.knowledge.evaluation.metrics import retrieval_metrics
from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RetrievalFilters, RetrievalResult, EvidenceChunk, RawHit
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.answer import CitationSnapshot, EvidenceBundle, RAGDecision

STRATEGIES=('dense','bm25','hybrid','hybrid_rerank')


async def _owned_thread(function, *args, **kwargs):
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


def _plain(value):
    if is_dataclass(value): return {k:_plain(v) for k,v in asdict(value).items()}
    if hasattr(value,'model_dump'): return _plain(value.model_dump())
    if isinstance(value,dict): return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [_plain(v) for v in value]
    if hasattr(value,'__dict__'): return {k:_plain(v) for k,v in vars(value).items()}
    return value


class EvaluationRunner:
    def __init__(self, *, snapshot, normalize, retrieve, gateway, judge, bundle_factory, store,
                 timeout_seconds=30, policy=None, ledger=None):
        self.snapshot,self.normalize,self.retrieve=snapshot,normalize,retrieve
        self.gateway,self.judge,self.bundle_factory,self.store=gateway,judge,bundle_factory,store
        self.timeout_seconds,self.policy,self.ledger=timeout_seconds,policy,ledger

    async def _guarded(self, eval_id, strategy, phase, operation):
        try:
            return await operation()
        except Exception as exc:
            code=getattr(exc,'code',None)
            self.store.record_error(eval_id,strategy,phase,code if isinstance(code,str) else phase+'_failed')
            raise

    async def run(self, mapped_cases, *, split):
        if split not in ('calibration','test'): raise ValueError('invalid split')
        selected=[m for m in mapped_cases if m.case.split==split]
        for mapped in selected:
            case=mapped.case
            saved=self.store.understanding(case.eval_id)
            if saved is None:
                deadline=time.monotonic()+self.timeout_seconds
                understood=await self._guarded(case.eval_id,'shared','understanding',
                    lambda:_owned_thread(self.normalize,case.query,deadline=deadline))
                saved=_plain(understood)
                self.store.save_understanding(case.eval_id,saved)
            else:
                understood=QueryUnderstanding(**saved)
            for strategy in STRATEGIES:
                current=next((r for r in self.store.rows() if (r['eval_id'],r['strategy'])==(case.eval_id,strategy)),None)
                if current and current['phase']=='complete': continue
                filters=RetrievalFilters(**case.filters)
                started=time.monotonic()
                resumed_partial=current is not None
                if current is None:
                    result=await self._guarded(case.eval_id,strategy,'retrieval',lambda:_owned_thread(
                        self.retrieve,understood,self.snapshot,strategy,filters,deadline=time.monotonic()+self.timeout_seconds))
                    ranked=[h.id for h in result.candidate_hits]
                    if strategy=='hybrid_rerank': ranked=[c.id for c in result.candidates]
                    metrics=retrieval_metrics(ranked,mapped.groups)
                    current={'eval_id':case.eval_id,'bucket':case.bucket,'difficulty':case.difficulty,'split':split,
                         'strategy':strategy,'phase':'retrieval','candidate_hits':_plain(result.candidate_hits),
                         'ranked_ids':ranked,'metrics':_plain(metrics),'candidates':_plain(result.candidates),
                         'candidate50_metrics':_plain(retrieval_metrics([h.id for h in result.candidate_hits],mapped.groups,ks=(50,))),
                         'answerable':case.answerable,'filters':case.filters}
                    self.store.checkpoint(current)
                else:
                    candidates=tuple(EvidenceChunk(c['id'],ChunkDraft(**c['draft']),c['rank'],c['score']) for c in current['candidates'])
                    hits=tuple(RawHit(**h) for h in current['candidate_hits'])
                    result=RetrievalResult(understood,self.snapshot,strategy,candidates,hits)
                if current['phase']=='retrieval' and (not result.candidates or (self.policy is not None and not self.policy.allows(result))):
                    self.store.checkpoint({'eval_id':case.eval_id,'strategy':strategy,'phase':'complete',
                        'status':'refused','reason':'retrieval_low_conf','answer':'','citations':[],
                        'faithfulness':None,'claims':[],'evidence_coverage':None,'elapsed_seconds':time.monotonic()-started,
                        'latency_incomplete':resumed_partial})
                    continue
                if current['phase']=='retrieval':
                    bundle=self.bundle_factory(result)
                    decision=await self._guarded(case.eval_id,strategy,'generation',lambda:self.gateway.answer(bundle))
                    bundle_ids={c.chunk_id for c in bundle.citations}
                    coverage=(sum(bool(bundle_ids & group) for group in mapped.groups)/len(mapped.groups)
                              if mapped.groups else None)
                    self.store.checkpoint({'eval_id':case.eval_id,'strategy':strategy,'phase':'generation',
                        'bundle':_plain(bundle),'decision':_plain(decision),'evidence_coverage':coverage})
                    current=next(r for r in self.store.rows() if (r['eval_id'],r['strategy'])==(case.eval_id,strategy))
                else:
                    data=current['bundle']
                    bundle=EvidenceBundle(data['question'],data['strategy'],data['corpus_hash'],
                        tuple(CitationSnapshot(**c) for c in data['citations']),tuple(tuple(m) for m in data['messages']),data['bundle_hash'])
                    decision=RAGDecision.model_validate(current['decision'])
                if not decision.useful:
                    self.store.checkpoint({'eval_id':case.eval_id,'strategy':strategy,'phase':'complete',
                        'status':'refused','reason':decision.reason,'answer':'','citations':[],
                        'faithfulness':None,'claims':[],'elapsed_seconds':time.monotonic()-started,'latency_incomplete':resumed_partial})
                    continue
                if self.judge is None: raise ValueError('judge required for answered cases')
                if current['phase']=='generation':
                    judged=await self._guarded(case.eval_id,strategy,'judging',lambda:self.judge.judge(decision.answer,bundle.citations))
                    self.store.checkpoint({'eval_id':case.eval_id,'strategy':strategy,'phase':'judging',
                        'faithfulness':judged.score,'claims':_plain(judged.claims)})
                else:
                    from retailq.knowledge.evaluation.judge import JudgeResult
                    judged=JudgeResult(tuple(current['claims']),current['faithfulness'])
                if self.ledger is not None and strategy=='hybrid_rerank' and any(not c['supported'] for c in judged.claims):
                    await self._guarded(case.eval_id,strategy,'ledger',lambda:self.ledger(case,decision,bundle,judged))
                self.store.checkpoint({'eval_id':case.eval_id,'strategy':strategy,'phase':'complete',
                    'status':'answered','answer':decision.answer,'citations':_plain(decision.citations),
                    'elapsed_seconds':time.monotonic()-started,'latency_incomplete':resumed_partial})
        return self.store.rows()
