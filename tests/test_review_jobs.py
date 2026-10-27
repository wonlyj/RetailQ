"""Catch admission races, canceled request ownership and premature resource closure."""
import asyncio
from threading import Event

import pytest
from sqlalchemy import update

from retailq.errors import ServiceError
from retailq.models import Message
from retailq.knowledge.review_contracts import DecisionCommand, PrepareCommand, ResumeCommand, SealCommand, evolve
from tests.support.reviews import RID, job_env, review_harness

pytestmark = [pytest.mark.mysql, pytest.mark.lite, pytest.mark.anyio]


async def entered(env):
    assert await asyncio.to_thread(env.entered.wait, 3)


async def test_accepted_command_is_durable_before_worker_finishes(job_env):
    env = job_env
    result = await env.jobs.submit(env.command)
    assert env.runtime.review_store.get_operation(result.operation_id).command == env.command
    await entered(env)
    assert env.h.knowledge.all_chunks() == []
    env.release.set()
    assert (await env.jobs.wait(result.operation_id)).phase == 'succeeded'
    assert env.runtime.review_store.get_run(RID).phase == 'waiting_review'


async def test_same_key_has_no_second_worker_and_changed_intent_conflicts(job_env):
    env = job_env
    await env.jobs.submit(env.command)
    await entered(env)
    repeated = await env.jobs.submit(env.command)
    assert repeated.operation_id == RID
    with pytest.raises(ServiceError) as exc:
        await env.jobs.submit(evolve(env.command, conversation_ids=None))
    assert exc.value.code == 'knowledge_request_conflict'
    with pytest.raises(ServiceError):
        await env.jobs.submit(evolve(env.command, request_key='b'*32))
    env.release.set()
    await env.jobs.wait(RID)
    assert env.gateway.extracted_refs == [env.source.source_ref]


async def test_shutdown_waits_for_admitted_worker_before_closing_resources(job_env, monkeypatch):
    env = job_env
    await env.jobs.submit(env.command)
    await entered(env)
    shutdown_entered = asyncio.Event()
    original = env.runtime.begin_review_shutdown
    def observed():
        original()
        shutdown_entered.set()
    monkeypatch.setattr(env.runtime, 'begin_review_shutdown', observed)
    closing = asyncio.create_task(env.jobs.aclose())
    await shutdown_entered.wait()
    assert not closing.done() and env.h.vectors.client is not None
    env.release.set()
    await asyncio.wait_for(closing, 5)
    assert env.exited.is_set()
    with pytest.raises(ServiceError):
        await env.jobs.submit(evolve(env.command, request_key='b'*32))


async def test_cancelled_request_still_launches_its_durable_operation(job_env, monkeypatch):
    env = job_env
    admitted, return_admission = Event(), Event()
    original = env.runtime.admit_review
    def observed(command):
        result = original(command)
        admitted.set()
        if not return_admission.wait(5):
            raise TimeoutError('test must release admission')
        return result
    monkeypatch.setattr(env.runtime, 'admit_review', observed)
    request = asyncio.create_task(env.jobs.submit(env.command))
    assert await asyncio.to_thread(admitted.wait, 3)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    return_admission.set()
    await entered(env)
    env.release.set()
    assert (await env.jobs.wait(RID)).phase == 'succeeded'


async def test_seal_waits_for_worker_then_releases_task_slot(job_env):
    env = job_env
    await env.jobs.submit(env.command)
    await entered(env)
    seal = SealCommand(request_key='c'*32, run_id=RID, confirm_run_id=RID)
    accepted = await env.jobs.seal(seal)
    assert accepted.operation_id == seal.request_key
    assert env.runtime.review_store.get_seal(RID) is None
    with pytest.raises(ServiceError):
        env.runtime.reserve_maintenance()
    env.release.set()
    assert (await env.jobs.wait(seal.request_key)).phase == 'sealed'
    assert env.exited.is_set() and env.runtime.review_store.unfinished() is None
    assert env.h.knowledge.all_chunks() == []
    with pytest.raises(ServiceError) as exc:
        await env.jobs.submit(env.command)
    assert exc.value.code == 'knowledge_review_sealed'


async def test_waiting_review_releases_maintenance_and_sql(job_env):
    env = job_env
    env.release.set()
    await env.jobs.submit(env.command)
    await env.jobs.wait(RID)
    assert env.h.database.engine.pool.checkedout() == 0
    lease = env.runtime.reserve_maintenance()
    lease.close(); lease.close()
    with pytest.raises(ServiceError) as exc:
        await env.jobs.submit(evolve(env.command, request_key='b'*32))
    assert exc.value.code == 'knowledge_review_pending'


async def test_faq_reads_while_model_waits_without_opening_another_lite(job_env, monkeypatch):
    import retailq.knowledge.vectors as vectors
    env = job_env
    def forbidden(*args, **kwargs):
        raise AssertionError('a second Milvus client must not be opened')
    monkeypatch.setattr(vectors, 'MilvusClient', forbidden)
    await env.jobs.submit(env.command)
    await entered(env)
    assert await asyncio.to_thread(env.runtime.search_faq, '怎么清洁') == []


async def test_queued_before_run_creation_resumes_original_frozen_source(job_env):
    env = job_env
    admission = env.runtime.admit_review(env.command)
    admission.lease.close()  # Lost process memory before worker launch; disk remains.
    assert env.runtime.review_store.unfinished().phase == 'interrupted'
    with env.h.database.sessions.begin() as session:
        session.execute(update(Message).where(Message.id == env.source.messages[0].id).values(content='后来的新问题'))
    env.release.set()
    resume = ResumeCommand(request_key='b'*32, run_id=RID)
    await env.jobs.submit(resume)
    assert (await env.jobs.wait(resume.request_key)).phase == 'succeeded'
    run = env.runtime.review_store.get_run(RID)
    group = env.runtime.review_store.get_group(RID, run.group_ids[0])
    assert group.questions == ('怎么清洁？',)


async def test_same_decision_with_new_key_is_durable_alias_and_does_not_need_chat(job_env):
    env = job_env
    env.release.set()
    await env.jobs.submit(env.command)
    await env.jobs.wait(RID)
    run = env.runtime.review_store.get_run(RID)
    group = env.runtime.review_store.get_group(RID, run.group_ids[0])
    env.runtime.gateway = None
    env.runtime.review_preparation = None
    command = DecisionCommand(request_key='d'*32, run_id=RID, group_id=group.group_id,
                              expected_revision=group.revision, decision='approve', category='售后手册')
    await env.jobs.submit(command)
    assert (await env.jobs.wait(command.request_key)).phase == 'succeeded'
    alias = evolve(command, request_key='e'*32)
    response = await env.jobs.submit(alias)
    assert response.operation_id == alias.request_key
    assert env.runtime.review_store.get_operation(alias.request_key).command == alias
    assert (await env.jobs.wait(alias.request_key)).phase == 'succeeded'
    with pytest.raises(ServiceError) as exc:
        await env.jobs.submit(evolve(env.command, request_key=alias.request_key))
    assert exc.value.code == 'knowledge_request_conflict'
    assert len(env.h.knowledge.all_chunks()) == 1


async def test_corrupt_queued_operation_does_not_leak_its_maintenance_lease(job_env):
    env = job_env
    admission = env.runtime.admit_review(env.command)
    path = env.h.store.root / 'reviews' / 'operations' / (RID + '.json')
    original = path.read_bytes()
    path.write_bytes(b'{broken')
    try:
        with pytest.raises(ServiceError):
            env.runtime.execute_review(RID, admission.lease, Event())
        assert admission.lease.done.is_set()
    finally:
        admission.lease.close()
        path.write_bytes(original)


async def test_failed_seal_write_can_retry_the_same_durable_request(job_env, monkeypatch):
    env = job_env
    env.release.set()
    await env.jobs.submit(env.command)
    await env.jobs.wait(RID)
    seal = SealCommand(request_key='c'*32, run_id=RID, confirm_run_id=RID)
    original = env.runtime.review_store.seal
    failures = []
    def fail_once(*args):
        if not failures:
            failures.append(True)
            raise OSError('disk failed before seal marker')
        return original(*args)
    monkeypatch.setattr(env.runtime.review_store, 'seal', fail_once)
    await env.jobs.seal(seal)
    assert (await env.jobs.wait(seal.request_key)).phase == 'failed'
    assert env.runtime.review_store.unfinished() is not None
    await env.jobs.seal(seal)
    assert (await env.jobs.wait(seal.request_key)).phase == 'sealed'
    assert env.runtime.review_store.unfinished() is None
