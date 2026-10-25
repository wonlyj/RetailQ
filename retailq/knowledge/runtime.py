from contextlib import ExitStack,contextmanager
from dataclasses import asdict
from pathlib import Path
import threading

from retailq.errors import ServiceError
from retailq.knowledge.embedding import SiliconFlowEmbedding
from retailq.knowledge.markdown import load_manifest
from retailq.knowledge.mining import MiningPipeline
from retailq.knowledge.ownership import DirectoryOwner
from retailq.knowledge.qa_gateway import OpenAIQAGateway
from retailq.knowledge.repository import KnowledgeRepository
from retailq.knowledge.service import KnowledgeService
from retailq.knowledge.token_budget import BGETokenBudget,TOKENIZER_REVISION,TOKENIZER_SHA256,verify_tokenizer


class KnowledgeRuntime:
    def __init__(self,service,owner,*,settings,embedding=None,vectors=None,gateway=None,pipeline=None):
        self.service,self.owner,self.settings=service,owner,settings
        self.root=owner.root
        self.embedding,self.vectors,self.gateway,self.pipeline=embedding,vectors,gateway,pipeline
        self._condition=threading.Condition()
        self._active=0;self._closing=False;self._closed=False
        self._read_slots=threading.BoundedSemaphore(2)
        self._maintenance=threading.Lock()

    @classmethod
    def open(cls,settings,database):
        if not settings.is_knowledge_configured:
            raise ServiceError('knowledge_not_configured',503,'请配置知识库数据库与独立嵌入服务。')
        try:
            with ExitStack() as stack:
                owner=stack.enter_context(DirectoryOwner(Path(settings.knowledge_data_dir)))
                verify_tokenizer(Path(settings.knowledge_tokenizer_path))
                budget=BGETokenBudget(Path(settings.knowledge_tokenizer_path))
                embedding=stack.enter_context(SiliconFlowEmbedding(settings,budget))
                # PyMilvus loads .env at import; never import it in disabled mode.
                from retailq.knowledge.vectors import MilvusKnowledgeStore
                identity={'provider':'siliconflow','model':settings.embedding_model,'endpoint':settings.embedding_base_url.rstrip('/'),
                    'dimension':'1024','metric':'COSINE','text_template_version':'1','normalization_version':'l2-v1',
                    'tokenizer_revision':TOKENIZER_REVISION,'tokenizer_sha256':TOKENIZER_SHA256,'schema_version':'1'}
                vectors=stack.enter_context(MilvusKnowledgeStore(owner.root/'knowledge.db',identity))
                repository=KnowledgeRepository(database)
                service=KnowledgeService(repository,embedding,vectors,budget,top_k=settings.knowledge_top_k,min_score=settings.knowledge_min_score,
                    batch_size=settings.embedding_batch_size,query_timeout_seconds=settings.tool_timeout_seconds,
                    chunk_target_chars=settings.knowledge_chunk_target,chunk_overlap_chars=settings.knowledge_chunk_overlap)
                gateway=stack.enter_context(OpenAIQAGateway(settings)) if settings.is_ready else None
                pipeline=MiningPipeline(repository,gateway,embedding,service,owner.root/'runs',input_budget=settings.input_budget,
                    min_score=settings.knowledge_dedup_min_score,candidate_k=settings.knowledge_dedup_candidate_k) if gateway else None
                instance=cls(service,owner,settings=settings,embedding=embedding,vectors=vectors,gateway=gateway,pipeline=pipeline)
                stack.pop_all()
                return instance
        except ServiceError:raise
        except Exception:
            raise ServiceError('knowledge_setup_failed',503,'知识库初始化未完成，请检查本地依赖、分词器与数据库配置。') from None

    @contextmanager
    def _operation(self):
        with self._condition:
            if self._closing:raise ServiceError('knowledge_closing',503,'知识服务正在关闭，请稍后重试。')
            self._active+=1
        try:yield
        finally:
            with self._condition:
                self._active-=1;self._condition.notify_all()

    def search_faq(self,keyword):
        if not self._read_slots.acquire(blocking=False):raise ServiceError('knowledge_busy',503,'知识查询繁忙，请稍后重试。')
        try:
            with self._operation():return self.service.search_faq(keyword)
        finally:self._read_slots.release()

    def maintain(self,action,options,stop):
        if not self._maintenance.acquire(blocking=False):raise ServiceError('knowledge_job_busy',409,'另一个知识维护任务正在执行。')
        try:
            with self._operation():
                if action=='import':
                    ids=self.service.import_faq() if options.get('faq') else self.service.import_documents(load_manifest(Path(options['manifest'])))
                    return {'imported_ids':ids,**self.service.status()}
                if action=='vectorize':return asdict(self.service.vectorize_pending(stop=stop))
                if action=='reconcile':return {'reset_ids':self.service.reconcile(),**self.service.status()}
                if action=='mine':
                    if self.pipeline is None:raise ServiceError('model_not_configured',503,'请先配置用于知识抽取的聊天模型。')
                    return asdict(self.pipeline.run(resume_run_id=options.get('resume'),stop=stop))
                if action=='status':return {**self.service.status(),'vector_verification':'not_checked'}
                raise ValueError('unsupported knowledge action')
        finally:self._maintenance.release()

    def close(self):
        with self._condition:
            if self._closing:
                while not self._closed:self._condition.wait()
                return
            self._closing=True
            while self._active:self._condition.wait()
        failed=False
        try:
            for resource in [self.gateway,self.vectors,self.embedding]:
                if resource is not None:
                    try:resource.close()
                    except Exception:failed=True
        finally:
            try:self.owner.__exit__(None,None,None)
            finally:
                with self._condition:self._closed=True;self._condition.notify_all()
        if failed:raise ServiceError('knowledge_close_failed',503,'知识资源关闭未完全确认，请核查本地进程。')
