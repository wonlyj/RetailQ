from contextlib import ExitStack,contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
import threading
import uuid

from retailq.errors import ServiceError
from retailq.knowledge.embedding import SiliconFlowEmbedding
from retailq.knowledge.markdown import load_manifest
from retailq.knowledge.ownership import DirectoryOwner
from retailq.knowledge.qa_gateway import OpenAIQAGateway
from retailq.knowledge.repository import KnowledgeRepository
from retailq.knowledge.service import KnowledgeService
from retailq.knowledge.token_budget import BGETokenBudget,TOKENIZER_REVISION,TOKENIZER_SHA256,verify_tokenizer
from retailq.knowledge.review_application import ReviewApplication
from retailq.knowledge.review_contracts import (
    AuditFault, DecisionCommand, OperationRecord, PrepareCommand, ResumeCommand, RetryVectorsCommand,
    RunRecord, SafeFailure, SealCommand, SelectionSummary, canonical_request_hash, evolve,
)
from retailq.knowledge.review_preparation import ReviewPreparation, processed_sources
from retailq.knowledge.review_queries import ReviewQueries
from retailq.knowledge.review_repository import ReviewRepository
from retailq.knowledge.review_store import ReviewStore, review_error


class MaintenanceLease:
    """One actual maintenance operation, counted until its real thread leaves."""
    def __init__(self, runtime, operation_id=None, run_id=None):
        self.runtime, self.operation_id, self.run_id = runtime, operation_id, run_id
        self.stop = threading.Event()
        self.done = threading.Event()

    def close(self):
        with self.runtime._admission:
            if self.done.is_set():
                return
            self.runtime._leases.discard(self)
            self.runtime._maintenance.release()
            with self.runtime._condition:
                self.runtime._active -= 1
                self.runtime._condition.notify_all()
            self.done.set()


@dataclass(frozen=True)
class Admission:
    operation: OperationRecord
    lease: MaintenanceLease | None


class KnowledgeRuntime:
    def __init__(self,service,owner,*,settings,embedding=None,vectors=None,gateway=None):
        self.service,self.owner,self.settings=service,owner,settings
        self.root=owner.root
        self.embedding,self.vectors,self.gateway=embedding,vectors,gateway
        self._condition=threading.Condition()
        self._active=0;self._closing=False;self._closed=False
        self._read_slots=threading.BoundedSemaphore(2)
        self._maintenance=threading.Lock()
        self._admission=threading.RLock()
        self._leases=set()
        self._review_closing=False
        self._pending_seal=None
        self.review_store=ReviewStore(self.root,clock=lambda:datetime.now(timezone(timedelta(hours=8))))
        self.review_repository=ReviewRepository(service.repository)
        self.review_application=ReviewApplication(service.repository,self.review_repository,self.review_store,service)
        self.review_queries=ReviewQueries(self.review_repository,service.repository,self.review_store,self.root/'runs')
        self._preparer=ReviewPreparation(service.repository,self.review_repository,self.review_store,gateway,embedding,
            input_budget=settings.input_budget,min_score=settings.knowledge_dedup_min_score,
            candidate_k=settings.knowledge_dedup_candidate_k)
        self.review_preparation=self._preparer if gateway is not None else None
        self._mark_interrupted()

    @property
    def can_prepare_review(self):
        return self.gateway is not None

    def _mark_interrupted(self):
        for operation in self.review_store.list_operations():
            if operation.phase not in ('queued','running') or operation.reused_operation_id:
                continue
            phase='sealed' if self.review_store.get_seal(operation.run_id) else 'interrupted'
            self._operation_phase(operation.operation_id,phase)
            if operation.kind=='seal' and phase!='sealed':
                self._pending_seal=operation.operation_id

    def _operation_phase(self, operation_id, phase, error=None):
        current=self.review_store.get_operation(operation_id)
        return self.review_store.put_operation(evolve(current,phase=phase,error=error),expected_revision=current.revision)

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
                instance=cls(service,owner,settings=settings,embedding=embedding,vectors=vectors,gateway=gateway)
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

    def _reserve(self, operation_id=None, run_id=None, *, seal=False):
        with self._condition:
            if self._closing or (self._review_closing and not seal):
                raise ServiceError('knowledge_closing',503,'知识服务正在关闭，请稍后重试。')
            if self._pending_seal and not seal:
                raise review_error('knowledge_job_busy','终止请求正在等待安全边界，请稍后重试。')
            if not self._maintenance.acquire(blocking=False):
                raise review_error('knowledge_job_busy','另一个知识维护任务正在执行。')
            self._active+=1
            lease=MaintenanceLease(self,operation_id,run_id)
            self._leases.add(lease)
            return lease

    def reserve_maintenance(self):
        with self._admission:
            return self._reserve()

    def active_leases(self):
        with self._admission:
            return tuple(self._leases)

    def begin_review_shutdown(self):
        with self._admission:
            self._review_closing=True
            for lease in self._leases:
                lease.stop.set()

    def _read_run(self, run_id, *, materialize=False):
        try:
            return self.review_store.get_run(run_id)
        except ServiceError as exc:
            if exc.status_code!=404:
                raise
        directory=self.review_store._path('runs',self.review_store._id(run_id))
        if directory.exists() and any(not p.name.startswith('.audit-') for p in directory.iterdir()):
            raise review_error('knowledge_review_corrupt','运行审计缺失但仍有组记录，请核查或终止并封存。')
        if (self.root/'runs'/f'run-{run_id}.json').exists():
            raise review_error('knowledge_legacy_readonly','旧版任务仅可查看，不能绕过人工审核继续发布。')
        original=self.review_store.get_operation(run_id)
        if original.kind!='prepare' or original.rule_hash is None:
            raise review_error('knowledge_review_corrupt','缺少准备任务冻结命令。')
        run=RunRecord(run_id=run_id,rule_hash=original.rule_hash,sources=original.source_snapshot,
            phase='interrupted',created_at=original.accepted_at,updated_at=original.accepted_at)
        return self.review_store.put_run(run,expected_revision=None) if materialize else run

    def _needs_preparation(self, run):
        if run.group_plan is None or len(run.completed_batches)!=len(run.batches):
            return True
        if tuple(g.group_id for g in run.group_plan)!=run.group_ids:
            return True
        for gid in run.group_ids:
            try:
                if self.review_store.get_group(run.run_id,gid).suggestion is None:
                    return True
            except ServiceError as exc:
                if exc.status_code==404:
                    return True
                raise
        return False

    def _known_operation(self, command):
        try:
            existing=self.review_store.get_operation(command.request_key)
        except ServiceError as exc:
            if exc.status_code==404:
                return None
            raise
        if existing.request_hash!=canonical_request_hash(command):
            raise review_error('knowledge_request_conflict','该请求键已用于其他操作。')
        return existing

    def _freeze_selection(self, command):
        processed=processed_sources(self.review_store)
        summaries=[]
        offset=0
        while True:
            page=self.review_repository.list_sources(eligibility='eligible',offset=offset,limit=100)
            summaries.extend(s for s in page.items if f'conversation:{s.id}:message:{s.max_message_id}' not in processed)
            offset+=len(page.items)
            if offset>=page.total:
                break
        if command.conversation_ids is None:
            ids=tuple(s.id for s in summaries[:100])
            sources=self.review_repository.freeze_sources(ids) if ids else ()
        else:
            sources=self.review_repository.freeze_sources(command.conversation_ids)
            if any(s.source_ref in processed for s in sources):
                raise review_error('knowledge_source_processed','所选来源已有完整处理记录，请刷新来源。')
        return sources,SelectionSummary(eligible_total=len(summaries),selected=len(sources),
                                        remaining=max(0,len(summaries)-len(sources)))

    def admit_review(self, command):
        with self._admission:
            existing=self._known_operation(command)
            run_id=command.request_key if isinstance(command,PrepareCommand) else command.run_id
            if self.review_store.get_seal(run_id):
                if isinstance(command,SealCommand) and existing:
                    return Admission(existing,None)
                raise review_error('knowledge_review_sealed','任务已终止并封存。')
            if self._review_closing and not (isinstance(command,SealCommand) and self._pending_seal==command.request_key):
                raise ServiceError('knowledge_closing',503,'知识服务正在关闭。')
            if existing:
                if (existing.reused_operation_id or existing.phase in ('succeeded','failed','sealed') or
                        any(l.operation_id==existing.operation_id for l in self._leases)):
                    return Admission(existing,None)
                lease=self._reserve(existing.operation_id,run_id,seal=isinstance(command,SealCommand))
                return Admission(existing,lease)
            if isinstance(command,SealCommand):
                raise review_error(message='终止操作必须先持久化排队。')
            if self._pending_seal:
                raise review_error('knowledge_job_busy','终止请求正在等待安全边界。')
            sources,selection,rule_hash=(),None,None
            alias=None
            if isinstance(command,PrepareCommand):
                if self.review_store.unfinished() is not None:
                    raise review_error('knowledge_review_pending','已有未结束审核任务，请先处理或终止并封存。')
                if not self.can_prepare_review:
                    raise ServiceError('model_not_configured',503,'请配置用于抽取和分类的聊天模型。')
                rule_hash=self._preparer.rule_hash()
            else:
                run=self._read_run(run_id)
                rule_hash=run.rule_hash
                if isinstance(command,ResumeCommand) and self._needs_preparation(run):
                    if rule_hash!=self._preparer.rule_hash():
                        raise review_error('knowledge_run_version_mismatch','准备规则已变化，请核查原任务。')
                    if not self.can_prepare_review:
                        raise ServiceError('model_not_configured',503,'当前未完成的准备阶段需要聊天模型。')
                if isinstance(command,DecisionCommand):
                    if command.group_id not in run.group_ids:
                        raise review_error('knowledge_review_not_found','审核组不存在。',404)
                    group=self.review_store.get_group(run_id,command.group_id)
                    decision=self.review_application._validated_decision(command,group)
                    if group.decision:
                        old=group.decision
                        if (old.kind,old.category,old.reason)!=(decision.kind,decision.category,decision.reason):
                            raise review_error(message='该组已有不同决定，不能覆盖。')
                        alias=self.review_store.get_operation(old.operation_id)
                    elif group.revision!=command.expected_revision:
                        raise review_error('knowledge_review_stale','审核组版本已变化，请刷新。')
            if alias:
                record=OperationRecord(operation_id=command.request_key,run_id=run_id,kind=command.kind,
                    command=command,request_hash=canonical_request_hash(command),rule_hash=rule_hash,
                    phase=alias.phase,accepted_at=self.review_store.timestamp(),reused_operation_id=alias.operation_id)
                return Admission(self.review_store.put_operation(record,expected_revision=None),None)
            lease=self._reserve(command.request_key,run_id)
            try:
                if isinstance(command,PrepareCommand):
                    sources,selection=self._freeze_selection(command)
                record=OperationRecord(operation_id=command.request_key,run_id=run_id,kind=command.kind,
                    command=command,request_hash=canonical_request_hash(command),source_snapshot=sources,
                    selection=selection,rule_hash=rule_hash,accepted_at=self.review_store.timestamp())
                return Admission(self.review_store.put_operation(record,expected_revision=None),lease)
            except BaseException:
                lease.close()
                raise

    def queue_review_seal(self, command):
        with self._admission:
            existing=self._known_operation(command)
            if existing:
                if not self.review_store.get_seal(command.run_id):
                    self._pending_seal=existing.reused_operation_id or existing.operation_id
                    canonical=self.review_store.get_operation(self._pending_seal)
                    if canonical.phase in ('failed','interrupted'):
                        canonical=self._operation_phase(canonical.operation_id,'queued')
                        if not existing.reused_operation_id:
                            existing=canonical
                return existing
            if self._review_closing or self._closing:
                raise ServiceError('knowledge_closing',503,'知识服务正在关闭。')
            # Read failure is acceptable only when a physical run/prepare command
            # exists: sealing preserves its bytes and never needs model access.
            path=self.review_store._path('runs',self.review_store._id(command.run_id))
            if not path.exists():
                self._read_run(command.run_id)
            alias=None
            if self._pending_seal:
                pending=self.review_store.get_operation(self._pending_seal)
                if pending.run_id!=command.run_id:
                    raise review_error('knowledge_job_busy','另一终止请求尚未完成。')
                alias=pending
            record=OperationRecord(operation_id=command.request_key,run_id=command.run_id,kind='seal',command=command,
                request_hash=canonical_request_hash(command),accepted_at=self.review_store.timestamp(),
                reused_operation_id=alias.operation_id if alias else None)
            record=self.review_store.put_operation(record,expected_revision=None)
            self._pending_seal=alias.operation_id if alias else record.operation_id
            for lease in self._leases:
                if lease.run_id==command.run_id:
                    lease.stop.set()
            return record

    def execute_review(self, operation_id, lease, stop):
        with self._admission:
            if lease.runtime is not self or lease not in self._leases or lease.operation_id!=operation_id:
                raise review_error(message='维护租约不属于该操作。')
            if lease.stop.is_set():
                stop.set()
            lease.stop=stop
        try:
            operation=self.review_store.get_operation(operation_id)
            self._operation_phase(operation_id,'running')
            command=operation.command
            if self.review_store.get_seal(operation.run_id) and not isinstance(command,SealCommand):
                return self._operation_phase(operation_id,'sealed')
            if isinstance(command,SealCommand):
                self.review_application.seal(operation_id)
                with self._admission:
                    self._pending_seal=None
            elif isinstance(command,PrepareCommand):
                self._read_run(operation.run_id,materialize=True)
                self._preparer.prepare(operation.run_id,stop=stop)
            elif isinstance(command,ResumeCommand):
                run=self._read_run(operation.run_id,materialize=True)
                if self._needs_preparation(run):
                    self._preparer.prepare(operation.run_id,stop=stop)
                # Accepted decisions may predate their worker. They are replayed
                # only after an explicit resume, under this same maintenance lease.
                queued=sorted(self.review_store.list_operations(),key=lambda o:(o.accepted_at,o.operation_id))
                for old in queued:
                    if (old.run_id==run.run_id and old.kind=='decide' and old.phase in ('queued','interrupted')
                            and not old.reused_operation_id):
                        try:
                            self.review_application.decide(old.operation_id,stop=stop)
                            self._operation_phase(old.operation_id,'succeeded')
                        except Exception as exc:
                            failure=self._safe_failure(exc)
                            self._operation_phase(old.operation_id,'failed',failure)
                            raise
                self.review_application.resume_applications(run.run_id,stop=stop)
            elif isinstance(command,DecisionCommand):
                self.review_application.decide(operation_id,stop=stop)
            elif isinstance(command,RetryVectorsCommand):
                self.review_application.retry_vectors(operation.run_id,stop=stop)
            return self._operation_phase(operation_id,'succeeded')
        except Exception as exc:
            return self._operation_phase(operation_id,'interrupted' if stop.is_set() else 'failed',self._safe_failure(exc))
        finally:
            lease.close()

    @staticmethod
    def _safe_failure(exc):
        return SafeFailure(code=exc.code if isinstance(exc,ServiceError) else 'knowledge_review_incomplete',
                           message=exc.public_message if isinstance(exc,ServiceError) else '任务未确认完成，请核查后继续。')

    def mining_report(self, run_id):
        run=self._read_run(run_id)
        view=self.review_queries.run(run_id).summary
        if view.counters is None:
            raise review_error('knowledge_review_corrupt','无法核对审核任务计数，请核查。')
        staged=self.service.repository.staging([b.batch_no for b in run.batches])
        original=self.review_store.get_operation(run_id)
        selection=original.selection
        return {'run_id':run.run_id,'extracted':view.counters.extracted,
            'kept':sum(r.status=='kept' for r in staged),'discarded':sum(r.status=='discarded' for r in staged),
            'vectorized':view.counters.vectorized,'complete':run.phase=='completed','phase':run.phase,
            'review_pending':view.counters.review_pending,
            'eligible_total':selection.eligible_total if selection else len(run.sources),
            'selected':selection.selected if selection else len(run.sources),
            'remaining':selection.remaining if selection else 0}

    def maintain(self,action,options,stop):
        if action=='mine':
            command=(ResumeCommand(request_key=uuid.uuid4().hex,run_id=options['resume']) if options.get('resume') else
                     PrepareCommand(request_key=uuid.uuid4().hex))
            try:
                admission=self.admit_review(command)
            except ServiceError as exc:
                if exc.code=='knowledge_review_pending' and not self.active_leases():
                    pending=self.review_store.unfinished()
                    if not isinstance(pending,AuditFault) and pending.phase=='waiting_review':
                        return self.mining_report(pending.run_id)
                raise
            operation=self.execute_review(admission.operation.operation_id,admission.lease,stop) if admission.lease else admission.operation
            report=self.mining_report(operation.run_id)
            if operation.error:
                report['error']=operation.error.model_dump()
            return report
        lease=self.reserve_maintenance()
        try:
            lease.stop=stop
            if action=='import':
                ids=self.service.import_faq() if options.get('faq') else self.service.import_documents(load_manifest(Path(options['manifest'])))
                return {'imported_ids':ids,**self.service.status()}
            if action=='vectorize':return asdict(self.service.vectorize_pending(stop=stop))
            if action=='reconcile':return {'reset_ids':self.service.reconcile(),**self.service.status()}
            if action=='status':return {**self.service.status(),'vector_verification':'not_checked'}
            raise ValueError('unsupported knowledge action')
        finally:lease.close()

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
