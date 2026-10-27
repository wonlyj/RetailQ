"""Lifespan-owned async wrappers; canceling a request never cancels a worker."""
import asyncio
from threading import Event

from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import SealCommand


class ReviewJobs:
    def __init__(self, runtime):
        self.runtime=runtime
        self._acceptances=set()
        self._workers={}
        self._closing=False

    async def submit(self, command):
        if isinstance(command,SealCommand):
            return await self.seal(command)
        if self._closing:
            raise ServiceError('knowledge_closing',503,'知识作业正在关闭。')
        task=asyncio.create_task(self._accept(command))
        self._acceptances.add(task)
        task.add_done_callback(self._acceptances.discard)
        return await asyncio.shield(task)

    async def _accept(self, command):
        admission=await asyncio.to_thread(self.runtime.admit_review,command)
        if admission.lease:
            self._launch(admission)
        return self.runtime.review_queries.operation(admission.operation.operation_id)

    def _launch(self, admission):
        operation_id=admission.operation.operation_id
        stop=admission.lease.stop
        if self._closing:
            stop.set()
        try:
            task=asyncio.create_task(asyncio.to_thread(self.runtime.execute_review,
                operation_id,admission.lease,stop))
            self._workers[operation_id]=task
        except BaseException:
            try:
                self.runtime._operation_phase(operation_id,'interrupted')
            finally:
                admission.lease.close()
            raise

    async def resume_queued(self, operation_id):
        return await self.submit(self.runtime.review_store.get_operation(operation_id).command)

    async def seal(self, command):
        if self._closing:
            raise ServiceError('knowledge_closing',503,'知识作业正在关闭。')
        task=asyncio.create_task(self._accept_seal(command))
        self._acceptances.add(task)
        task.add_done_callback(self._acceptances.discard)
        return await asyncio.shield(task)

    async def _accept_seal(self, command):
        operation=await asyncio.to_thread(self.runtime.queue_review_seal,command)
        canonical=operation.reused_operation_id or operation.operation_id
        original=self.runtime.review_store.get_operation(canonical)
        previous=self._workers.get(canonical)
        if self.runtime.review_store.get_seal(command.run_id) is None:
            if previous is None or previous.done():
                self._workers[canonical]=asyncio.create_task(self._finish_seal(original.command))
            else:
                # An explicit retry may arrive after a failed marker write but
                # just before that thread has left its finally block.
                def retry_if_queued(finished):
                    if self._workers.get(canonical) is not finished:
                        return
                    try:
                        current=self.runtime.review_store.get_operation(canonical)
                        if current.phase=='queued' and self.runtime.review_store.get_seal(current.run_id) is None:
                            self._workers[canonical]=asyncio.create_task(self._finish_seal(current.command))
                    except ServiceError:
                        pass  # Corruption stays visible; never fabricate a marker.
                previous.add_done_callback(retry_if_queued)
        return self.runtime.review_queries.operation(operation.operation_id)

    async def _finish_seal(self, command):
        for lease in self.runtime.active_leases():
            await asyncio.to_thread(lease.done.wait)
        admission=await asyncio.to_thread(self.runtime.admit_review,command)
        if admission.lease:
            # Keep the async owner until the synchronous thread has really left.
            return await asyncio.to_thread(self.runtime.execute_review,command.request_key,
                                            admission.lease,Event())
        return admission.operation

    async def wait(self, operation_id):
        # Also join acceptance tasks: a disconnected caller may have canceled
        # before admission returned and before its worker had been registered.
        while self._acceptances:
            await asyncio.shield(asyncio.gather(*tuple(self._acceptances),return_exceptions=True))
        operation=self.runtime.review_store.get_operation(operation_id)
        canonical=operation.reused_operation_id or operation_id
        while task:=self._workers.get(canonical):
            await asyncio.shield(task)
            if self._workers.get(canonical) is task:
                break
        return self.runtime.review_queries.operation(operation_id)

    async def aclose(self):
        self._closing=True
        self.runtime.begin_review_shutdown()
        while self._acceptances:
            await asyncio.shield(asyncio.gather(*tuple(self._acceptances),return_exceptions=True))
        for lease in self.runtime.active_leases():
            lease.stop.set()
        if self._workers:
            await asyncio.shield(asyncio.gather(*tuple(self._workers.values()),return_exceptions=True))
        # Runtime also owns leases used synchronously by CLI/scheduler callers.
        for lease in self.runtime.active_leases():
            await asyncio.to_thread(lease.done.wait)
