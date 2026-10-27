import asyncio
from datetime import datetime,timedelta
import json
import threading
import uuid
from zoneinfo import ZoneInfo

from retailq.errors import ServiceError
from retailq.knowledge.mining import atomic_json
from retailq.knowledge.review_contracts import AuditFault, PrepareCommand

SHANGHAI=ZoneInfo('Asia/Shanghai')


def next_run_at(now,*,hour=3,minute=0):
    if now.tzinfo is None:raise ValueError('schedule clock must be timezone aware')
    local=now.astimezone(SHANGHAI)
    candidate=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    return candidate+timedelta(days=1) if candidate<=local else candidate


class DailyKnowledgeScheduler:
    def __init__(self,runtime,jobs=None,*,clock=None):
        self.runtime=runtime
        self.jobs=jobs
        self.operation_id=None
        self.clock=clock or (lambda:datetime.now(SHANGHAI))
        self.path=runtime.root/'schedule.json'
        state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.last_attempt=None if state.get('phase')=='running' else state.get('last_attempt')
        self.stop_event=threading.Event();self.wakeup=asyncio.Event()
        self.task=None;self.worker=None;self.stopping=False

    def start(self):
        if self.runtime.settings.knowledge_schedule_enabled and self.task is None:
            self.task=asyncio.create_task(self._loop())

    async def run_due(self):
        if self.stopping or not self.runtime.settings.knowledge_schedule_enabled:return
        now=self.clock().astimezone(SHANGHAI)
        due=now.replace(hour=self.runtime.settings.knowledge_schedule_hour,minute=self.runtime.settings.knowledge_schedule_minute,second=0,microsecond=0)
        day=now.date().isoformat()
        if now<due or self.last_attempt==day:return
        self.last_attempt=day
        state={'last_attempt':day,'phase':'running','complete':False}
        atomic_json(self.path,state)
        self.worker=asyncio.create_task(self._mine())
        try:
            result=await asyncio.shield(self.worker)
            state.update(phase=result.get('phase') or ('complete' if result.get('complete') else 'incomplete'),
                         complete=bool(result.get('complete')),run_id=result.get('run_id'),report=result)
        except asyncio.CancelledError:
            self.stop_event.set()
            await asyncio.shield(self.worker)
            state.update(phase='stopped',complete=False,error_code='knowledge_job_stopped')
            raise
        except Exception as exc:
            state.update(phase='failed',complete=False,error_code=exc.code if isinstance(exc,ServiceError) else 'knowledge_schedule_failed')
        finally:
            atomic_json(self.path,state)
            self.worker=None

    async def _mine(self):
        if self.jobs is None:
            return await asyncio.to_thread(self.runtime.maintain,'mine',{},self.stop_event)
        try:
            operation=await self.jobs.submit(PrepareCommand(request_key=uuid.uuid4().hex))
        except ServiceError as exc:
            if exc.code=='knowledge_review_pending' and not self.runtime.active_leases():
                pending=self.runtime.review_store.unfinished()
                if pending is not None and not isinstance(pending,AuditFault) and pending.phase=='waiting_review':
                    return await asyncio.to_thread(self.runtime.mining_report,pending.run_id)
            raise
        self.operation_id=operation.operation_id
        if self.stopping:
            self._stop_current()
        await self.jobs.wait(operation.operation_id)
        return await asyncio.to_thread(self.runtime.mining_report,operation.run_id)

    def _stop_current(self):
        if self.jobs is not None and self.operation_id:
            for lease in self.runtime.active_leases():
                if lease.operation_id==self.operation_id:
                    lease.stop.set()

    async def _loop(self):
        while not self.stopping:
            await self.run_due()
            if self.stopping:break
            delay=max(0.01,(next_run_at(self.clock(),hour=self.runtime.settings.knowledge_schedule_hour,minute=self.runtime.settings.knowledge_schedule_minute)-self.clock().astimezone(SHANGHAI)).total_seconds())
            try:await asyncio.wait_for(self.wakeup.wait(),timeout=delay)
            except TimeoutError:pass

    async def stop(self):
        self.stopping=True;self.stop_event.set();self.wakeup.set()
        self._stop_current()
        pending=self.task or self.worker
        if pending is not None:
            try:await asyncio.shield(pending)
            except asyncio.CancelledError:
                if self.worker is not None:await asyncio.shield(self.worker)
