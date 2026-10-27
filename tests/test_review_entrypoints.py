"""Catch CLI/scheduler bypasses, misleading success codes and lost audit recovery."""
from datetime import datetime, timezone
import json
from threading import Event

import pytest

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import DecisionCommand, PrepareCommand, ResumeCommand
from tests.support.reviews import (
    RID, review_harness, job_env, seed_closed_source, review_runtime_factory,
)

pytestmark = [pytest.mark.mysql, pytest.mark.lite]


def cli_settings(root):
    return Settings(mysql_port=3307, mysql_database='retailq_test', knowledge_data_dir=str(root),
                    embedding_api_key='test', context_window_tokens=31536, knowledge_schedule_enabled=False)


def test_cli_mine_waits_for_review_with_zero_exit(review_harness, capsys):
    from scripts.knowledge import main
    h = review_harness
    seed_closed_source(h.database)
    settings = cli_settings(h.store.root / 'cli')
    before = h.knowledge.all_chunks()
    assert main(['mine'], settings=settings, runtime_factory=review_runtime_factory) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['phase'] == 'waiting_review' and report['complete'] is False
    assert report['selected'] == 1 and report['remaining'] == 0
    assert h.knowledge.all_chunks() == before
    # A second invocation reuses the waiting task, not the same sources again.
    assert main(['mine'], settings=settings, runtime_factory=review_runtime_factory) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == report['run_id']


def test_cli_old_task_is_readonly_and_returns_configuration_code(review_harness, capsys):
    from scripts.knowledge import main
    h = review_harness
    settings = cli_settings(h.store.root / 'cli')
    path = h.store.root / 'cli' / 'runs' / ('run-' + RID + '.json')
    path.parent.mkdir(parents=True)
    original = b'{"complete":false,"phase":"vectorize"}'
    path.write_bytes(original)
    assert main(['mine', '--resume', RID], settings=settings, runtime_factory=review_runtime_factory) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['error']['code'] == 'knowledge_legacy_readonly'
    assert path.read_bytes() == original and h.knowledge.all_chunks() == []


@pytest.mark.anyio
async def test_scheduler_prepares_once_then_waits_even_after_restart(job_env):
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    env = job_env
    env.release.set()
    env.runtime.settings = env.runtime.settings.model_copy(update={'knowledge_schedule_enabled':True})
    clock = lambda: datetime(2026, 10, 27, 5, tzinfo=timezone.utc)
    scheduler = DailyKnowledgeScheduler(env.runtime, jobs=env.jobs, clock=clock)
    await scheduler.run_due(); await scheduler.run_due()
    replacement = DailyKnowledgeScheduler(env.runtime, jobs=env.jobs, clock=clock)
    await replacement.run_due()
    state = json.loads(scheduler.path.read_text(encoding='utf-8'))
    assert state['phase'] == 'waiting_review' and state['complete'] is False
    assert env.gateway.extracted_refs == [env.source.source_ref]
    assert env.h.knowledge.all_chunks() == []


@pytest.mark.anyio
async def test_scheduler_and_cli_cannot_bypass_active_page_job(job_env):
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    env = job_env
    await env.jobs.submit(env.command)
    assert await __import__('asyncio').to_thread(env.entered.wait, 3)
    with pytest.raises(ServiceError):
        env.runtime.maintain('mine', {}, Event())
    env.runtime.settings = env.runtime.settings.model_copy(update={'knowledge_schedule_enabled':True})
    scheduler = DailyKnowledgeScheduler(env.runtime, jobs=env.jobs,
        clock=lambda:datetime(2026, 10, 27, 5, tzinfo=timezone.utc))
    await scheduler.run_due()
    assert env.h.knowledge.all_chunks() == []
    env.release.set()
    await env.jobs.wait(RID)
    assert len(env.gateway.extracted_refs) == 1


@pytest.mark.anyio
async def test_default_selection_reports_remaining_over_hundred(job_env):
    env = job_env
    for i in range(100):
        seed_closed_source(env.h.database, question='问'+str(i))
    admission = env.runtime.admit_review(PrepareCommand(request_key=RID))
    try:
        selection = admission.operation.selection
        assert (selection.eligible_total, selection.selected, selection.remaining) == (101, 100, 1)
        assert len(admission.operation.source_snapshot) == 100
        assert admission.operation.source_snapshot[0].conversation_id == env.source.conversation_id
    finally:
        admission.lease.close()


@pytest.mark.anyio
async def test_missing_published_run_audit_cannot_be_recreated_as_unreviewed(job_env):
    env = job_env
    env.release.set()
    await env.jobs.submit(env.command)
    await env.jobs.wait(RID)
    run = env.runtime.review_store.get_run(RID)
    group = env.runtime.review_store.get_group(RID, run.group_ids[0])
    command = DecisionCommand(request_key='d'*32, run_id=RID, group_id=group.group_id,
                              expected_revision=group.revision, decision='approve', category='售后手册')
    await env.jobs.submit(command); await env.jobs.wait(command.request_key)
    path = env.h.store.root / 'reviews' / 'runs' / RID / 'run.json'
    original = path.read_bytes()
    path.unlink()
    admission = None
    try:
        with pytest.raises(ServiceError) as exc:
            admission = env.runtime.admit_review(ResumeCommand(request_key='e'*32, run_id=RID))
        assert exc.value.code == 'knowledge_review_corrupt'
        assert len(env.h.knowledge.all_chunks()) == 1
    finally:
        if admission and admission.lease:
            admission.lease.close()
        path.write_bytes(original)
