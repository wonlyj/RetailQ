from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re

from retailq.errors import ServiceError
from retailq.knowledge.contracts import MiningReport,ConversationSnapshot,MiningMessage
from retailq.knowledge.dedup import global_dedup
from retailq.knowledge.extraction import batch_conversations,extract_to_staging
from retailq.knowledge.qa_gateway import prompt


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)


def snapshot_from_json(data):
    return ConversationSnapshot(data['source_ref'],data['conversation_id'],data['max_message_id'],tuple(MiningMessage(**m) for m in data['messages']))


class MiningPipeline:
    def __init__(self,repository,gateway,embedding,service,run_root: Path,*,input_budget=6656,max_conversations=20,min_score=0.85,candidate_k=8):
        self.repository,self.gateway,self.embedding,self.service=repository,gateway,embedding,service
        self.root=Path(run_root)
        self.input_budget,self.max_conversations=input_budget,max_conversations
        self.min_score,self.candidate_k=min_score,candidate_k

    def run(self,*,resume_run_id=None,stop=None):
        self.root.mkdir(parents=True,exist_ok=True)
        version=hashlib.sha256((prompt('qa_extract.txt')+prompt('qa_dedup.txt')+'pipeline-v1').encode('utf-8')).hexdigest()
        manifests=[]
        for path in sorted(self.root.glob('run-*.json'),key=lambda p:p.stat().st_mtime):
            manifests.append((path,json.loads(path.read_text(encoding='utf-8'))))
        if resume_run_id is not None:
            if not re.fullmatch(r'[0-9a-f]{32}',resume_run_id):raise ValueError('invalid run id')
            path=self.root/f'run-{resume_run_id}.json'
            metadata=json.loads(path.read_text(encoding='utf-8'))
            if metadata['prompt_hash']!=version:raise ServiceError('knowledge_run_version_mismatch',409,'抽取规则版本已变化，请核查原任务后重跑。')
        else:
            unfinished=next(((p,m) for p,m in manifests if not m['complete'] and m['prompt_hash']==version),None)
            if unfinished:
                path,metadata=unfinished
            else:
                completed={s['source_ref'] for _,m in manifests if m['complete'] and m['prompt_hash']==version for s in m['sources']}
                sources=[s for s in self.repository.closed_conversations() if s.source_ref not in completed]
                signature=json.dumps({'sources':[asdict(s) for s in sources],'prompt':version,'budget':self.input_budget,'max_conversations':self.max_conversations},ensure_ascii=False,sort_keys=True)
                run_id=hashlib.sha256(signature.encode('utf-8')).hexdigest()[:32]
                path=self.root/f'run-{run_id}.json'
                batches=batch_conversations(sources,max_conversations=self.max_conversations,input_budget=self.input_budget)
                metadata={'run_id':run_id,'prompt_hash':version,'sources':[asdict(s) for s in sources],
                    'batches':[{'batch_no':hashlib.sha256(f'{run_id}:{i}'.encode()).hexdigest(),'sources':[asdict(s) for s in batch]} for i,batch in enumerate(batches)],
                    'completed_batches':[],'dedup_complete':False,'complete':False,'phase':'extraction','error_code':None}
                atomic_json(path,metadata)
        vectorized=0
        try:
            for batch in metadata['batches']:
                if stop is not None and stop.is_set():raise ServiceError('knowledge_job_stopped',409,'任务已停止。')
                if batch['batch_no'] in metadata['completed_batches']:continue
                extract_to_staging(self.repository,self.gateway,[snapshot_from_json(s) for s in batch['sources']],batch['batch_no'])
                metadata['completed_batches'].append(batch['batch_no']);atomic_json(path,metadata)
            metadata['phase']='dedup';atomic_json(path,metadata)
            batch_nos=[b['batch_no'] for b in metadata['batches']]
            staged=self.repository.staging(batch_nos)
            if stop is not None and stop.is_set():raise ServiceError('knowledge_job_stopped',409,'任务已停止。')
            groups=global_dedup(staged,self.repository.all_chunks(),self.embedding,self.gateway,min_score=self.min_score,candidate_k=self.candidate_k,stop=stop)
            metadata['dedup_groups']=[asdict(group) for group in groups]
            metadata['dedup_parameters']={'min_score':self.min_score,'candidate_k':self.candidate_k,'comparison':'direct_pairs'}
            atomic_json(path,metadata)
            self.repository.apply_dedup(groups)
            metadata['dedup_complete']=True;metadata['phase']='vectorize';atomic_json(path,metadata)
            report=self.service.vectorize_pending(stop=stop)
            vectorized=report.done
            metadata['complete']=report.failed==0 and report.pending_after==0 and not (stop is not None and stop.is_set())
            metadata['phase']='complete' if metadata['complete'] else 'vectorize'
            metadata['error_code']=None if metadata['complete'] else 'knowledge_vectorize_incomplete'
            metadata['vector_failures']=[asdict(f) for f in report.failures]
        except Exception as exc:
            metadata['complete']=False
            metadata['error_code']=exc.code if isinstance(exc,ServiceError) else 'knowledge_mining_failed'
        atomic_json(path,metadata)
        staged=self.repository.staging([b['batch_no'] for b in metadata['batches']])
        return MiningReport(metadata['run_id'],len(staged),sum(r.status=='kept' for r in staged),sum(r.status=='discarded' for r in staged),vectorized,metadata['complete'])
