from datetime import datetime, timezone
from types import SimpleNamespace

from retailq.config import Settings
from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.knowledge.repository import KnowledgeRepository
from retailq.knowledge.review_contracts import evolve
from retailq.knowledge.review_store import ReviewStore
from retailq.knowledge.runtime import KnowledgeRuntime
from tests.support.reviews import make_operation
import pytest


def test_new_worker_does_not_interrupt_live_operation(tmp_path):
    root = tmp_path/'knowledge'
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
    store = ReviewStore(root, clock=lambda:datetime.now(timezone.utc))
    operation = evolve(make_operation(), phase='running')
    store.put_operation(operation, expected_revision=None)
    live = coordinator.try_acquire('maintenance')
    assert live is not None
    owner = SimpleNamespace(root=root, __exit__=lambda *args: None)
    service = SimpleNamespace(repository=KnowledgeRepository(None))
    settings = Settings(_env_file=None, knowledge_data_dir=str(root))
    try:
        worker = KnowledgeRuntime(service, owner, settings=settings, coordinator=coordinator)
        assert worker.review_store.get_operation(operation.operation_id).phase == 'running'
    finally:
        live.close()
    orphan = KnowledgeRuntime(service, owner, settings=settings, coordinator=coordinator)
    assert orphan.review_store.get_operation(operation.operation_id).phase == 'interrupted'


@pytest.mark.anyio
async def test_scheduler_shared_leader_runs_day_once(tmp_path):
    import asyncio
    from datetime import timedelta
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    root = tmp_path/'knowledge'; root.mkdir()
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
    settings = Settings(_env_file=None, knowledge_schedule_enabled=True)
    runtime = SimpleNamespace(root=root, settings=settings, coordinator=coordinator)
    now = datetime(2026,10,29,12,tzinfo=timezone(timedelta(hours=8)))
    first, second = DailyKnowledgeScheduler(runtime,clock=lambda:now), DailyKnowledgeScheduler(runtime,clock=lambda:now)
    entered, finish, calls = asyncio.Event(), asyncio.Event(), []
    async def mine():
        calls.append('run'); entered.set(); await finish.wait(); return {'complete':True}
    first._mine = second._mine = mine
    task = asyncio.create_task(first.run_due())
    await entered.wait()
    await second.run_due()
    assert calls == ['run']
    finish.set(); await task
    await second.run_due()
    assert calls == ['run']
    # A different worker becomes leader for the next due day, without
    # replaying the already completed day.
    now += timedelta(days=1)
    await second.run_due()
    assert calls == ['run', 'run']


def test_running_operation_is_preserved_across_processes(tmp_path):
    import subprocess
    import sys
    root=tmp_path/'knowledge'
    identity=RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root)
    coordinator=Coordinator(tmp_path/'locks',identity)
    store=ReviewStore(root,clock=lambda:datetime.now(timezone.utc),coordinator=coordinator)
    operation=evolve(make_operation(),phase='running')
    store.put_operation(operation,expected_revision=None)
    code = "from pathlib import Path; from retailq.coordination import Coordinator,RuntimeIdentity; import sys; c=Coordinator(Path(sys.argv[1]),RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',Path(sys.argv[2]))); lease=c.try_acquire('maintenance'); print('ready' if lease else 'busy',flush=True); input(); lease.close()"
    process=subprocess.Popen([sys.executable,'-c',code,str(tmp_path/'locks'),str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        owner=SimpleNamespace(root=root,__exit__=lambda *args:None)
        worker=KnowledgeRuntime(SimpleNamespace(repository=KnowledgeRepository(None)),owner,
                                settings=Settings(_env_file=None,knowledge_data_dir=str(root)),coordinator=coordinator)
        assert worker.review_store.get_operation(operation.operation_id).phase=='running'
    finally:
        process.communicate(input='\n',timeout=10)
    assert process.returncode==0
