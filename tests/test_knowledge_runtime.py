import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
import threading

import pytest

from retailq.config import Settings
from retailq.errors import ServiceError


def runtime(tmp_path,service=None,pipeline=None):
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    class Resource:
        def __init__(self):self.closed=False
        def close(self):self.closed=True
    class Service:
        def search_faq(self,text):return [{'answer':'事实'}]
        def status(self):return {'pending':0,'done':0,'chunks':0}
    settings=Settings(_env_file=None,knowledge_enabled=True,knowledge_data_dir=str(tmp_path))
    return KnowledgeRuntime(service or Service(),DirectoryOwner(tmp_path).__enter__(),settings=settings,embedding=Resource(),vectors=Resource(),pipeline=pipeline)


def test_schedule_uses_shanghai_not_host_timezone():
    from retailq.knowledge.scheduler import next_run_at
    result=next_run_at(datetime(2026,10,25,20,tzinfo=timezone.utc))
    assert result.isoformat()=='2026-10-27T03:00:00+08:00'


@pytest.mark.anyio
async def test_late_start_runs_once_without_replaying_missed_days(tmp_path):
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    calls=[]
    class Runtime:
        root=tmp_path
        settings=Settings(_env_file=None,knowledge_enabled=True)
        def maintain(self,action,options,stop):calls.append(action);return {'complete':True,'pending':0}
    clock=lambda:datetime(2026,10,25,5,tzinfo=timezone.utc)
    scheduler=DailyKnowledgeScheduler(Runtime(),clock=clock)
    await scheduler.run_due();await scheduler.run_due()
    replacement=DailyKnowledgeScheduler(Runtime(),clock=clock)
    await replacement.run_due()
    assert calls==['mine']


def test_timed_out_faq_thread_keeps_resources_until_done(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Service:
        def search_faq(self,text):entered.set();release.wait(3);return []
    instance=runtime(tmp_path,Service())
    with ThreadPoolExecutor(2) as executor:
        query=executor.submit(instance.search_faq,'问题')
        assert entered.wait(2)
        closed=executor.submit(instance.close)
        assert not instance.vectors.closed
        with pytest.raises(ServiceError):instance.search_faq('关闭后新问题')
        release.set();query.result(timeout=3);closed.result(timeout=3)
    assert instance.vectors.closed and instance.embedding.closed


def test_maintenance_is_single_job_while_reads_remain_bounded(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Pipeline:
        def run(self,**kwargs):
            from retailq.knowledge.contracts import MiningReport
            entered.set();release.wait(3)
            return MiningReport('x',0,0,0,0,True)
    instance=runtime(tmp_path,pipeline=Pipeline())
    try:
        with ThreadPoolExecutor(1) as executor:
            job=executor.submit(instance.maintain,'mine',{},threading.Event())
            assert entered.wait(2)
            with pytest.raises(ServiceError):instance.maintain('mine',{},threading.Event())
            assert instance.search_faq('问题')
            release.set();job.result(timeout=3)
    finally:release.set();instance.close()


@pytest.mark.anyio
async def test_cancelling_scheduler_waits_for_worker_before_close(tmp_path):
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    entered=threading.Event();done=threading.Event()
    class Runtime:
        root=tmp_path
        settings=Settings(_env_file=None,knowledge_enabled=True)
        def maintain(self,action,options,stop):
            entered.set();stop.wait(3);done.set();return {'complete':False}
    scheduler=DailyKnowledgeScheduler(Runtime(),clock=lambda:datetime(2026,10,25,5,tzinfo=timezone.utc))
    scheduler.start()
    assert await asyncio.to_thread(entered.wait,2)
    await scheduler.stop()
    assert done.is_set()


def test_disabled_or_missing_config_reports_not_ready(tmp_path):
    from retailq.knowledge.runtime import KnowledgeRuntime
    settings=Settings(_env_file=None,knowledge_data_dir=str(tmp_path/'new'))
    assert not settings.is_knowledge_configured
    with pytest.raises(ServiceError):KnowledgeRuntime.open(settings,None)
    assert not (tmp_path/'new').exists()


def test_open_failure_releases_directory_and_clients(tmp_path):
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    settings=Settings(_env_file=None,knowledge_enabled=True,mysql_password='test',embedding_api_key='test',knowledge_data_dir=str(tmp_path),knowledge_tokenizer_path=str(tmp_path/'missing.json'))
    with pytest.raises(ServiceError):KnowledgeRuntime.open(settings,None)
    with DirectoryOwner(tmp_path):pass


@pytest.mark.mysql
@pytest.mark.lite
def test_stop_between_batches_leaves_recoverable_pending(service):
    from retailq.knowledge.contracts import ChunkDraft
    service.repository.import_chunks([ChunkDraft('知识','问','答案。','stop1','manual'),ChunkDraft('知识','问','另一个答案。','stop2','manual')])
    stop=threading.Event();service.batch_size=1;original=service.embedding.embed
    def embed(texts,**kwargs):stop.set();return original(texts,**kwargs)
    service.embedding.embed=embed
    report=service.vectorize_pending(stop=stop)
    assert report.done==1 and report.pending_after==1


from tests.support.knowledge import service
