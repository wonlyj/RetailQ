import json
from types import SimpleNamespace

import pytest

from scripts.verify_rag_concurrency import wait_for_owned_phase


def test_harness_rejects_unreached_kill_phase(tmp_path):
    path=tmp_path/'ready.json'
    path.write_text(json.dumps({'pid':123,'phase':'wrong'}),encoding='utf-8')
    child=SimpleNamespace(pid=123,poll=lambda:None)
    with pytest.raises(ValueError,match='phase'):
        wait_for_owned_phase(path,child,'target',timeout=.1)


def test_harness_rejects_foreign_or_exited_process(tmp_path):
    path=tmp_path/'ready.json'
    path.write_text(json.dumps({'pid':456,'phase':'target'}),encoding='utf-8')
    with pytest.raises(ValueError,match='identity'):
        wait_for_owned_phase(path,SimpleNamespace(pid=123,poll=lambda:None),'target',timeout=.1)
    path.write_text(json.dumps({'pid':123,'phase':'target'}),encoding='utf-8')
    with pytest.raises(ValueError,match='exited'):
        wait_for_owned_phase(path,SimpleNamespace(pid=123,poll=lambda:0),'target',timeout=.1)


def test_actual_owned_process_death_releases_maintenance_lease(tmp_path):
    import os
    import subprocess
    import sys
    from retailq.coordination import Coordinator,RuntimeIdentity

    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json'
    env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt': env['__PYVENV_LAUNCHER__']=sys.executable
    process=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),'--marker',str(marker)],env=env)
    try:
        wait_for_owned_phase(marker,process,'maintenance_held',timeout=10)
        identity=RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root)
        coordinator=Coordinator(locks,identity)
        assert coordinator.try_acquire('maintenance') is None
        process.kill();process.wait(timeout=10)
        lease=coordinator.try_acquire('maintenance')
        assert lease is not None
        lease.close()
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=10)


def test_harness_refuses_output_outside_evidence_root(tmp_path):
    from scripts.verify_rag_concurrency import validate_output
    with pytest.raises(ValueError): validate_output(tmp_path/'outside')


def test_harness_real_mode_delegates_to_live_checks(monkeypatch):
    import scripts.verify_rag_concurrency as harness
    seen=[]
    monkeypatch.setattr(harness,'run_real_checks',lambda output:seen.append(output) or {'passed':True},raising=False)
    assert harness.main(['--mode','real','--output','artifacts/verification/ch04/real-example'])==0
    assert seen==['artifacts/verification/ch04/real-example']


def test_harness_real_mode_accepts_isolated_alternate_port(monkeypatch):
    import scripts.verify_rag_concurrency as harness
    seen=[]
    monkeypatch.setattr(harness,'run_real_checks',lambda output,base_url:seen.append((output,base_url)) or {'passed':True})
    assert harness.main(['--mode','real','--output','artifacts/verification/ch04/real-example','--base-url','http://127.0.0.1:18801'])==0
    assert seen==[('artifacts/verification/ch04/real-example','http://127.0.0.1:18801')]


def test_conversation_leases_are_exclusive_per_id_across_processes(tmp_path):
    import os, subprocess, sys
    root=tmp_path/'knowledge';locks=tmp_path/'locks';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    def start(name,key):
        marker=tmp_path/(name+'.json')
        child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
                                '--marker',str(marker),'--scope','conversation','--key',key],env=env)
        return child,marker
    children=[]
    try:
        first,mark1=start('first','42');children.append(first)
        wait_for_owned_phase(mark1,first,'conversation_held',timeout=10)
        same,marks=start('same','42');children.append(same)
        assert same.wait(timeout=10)==2
        assert not marks.exists()
        other,mark2=start('other','43');children.append(other)
        wait_for_owned_phase(mark2,other,'conversation_held',timeout=10)
        assert first.poll() is None and other.poll() is None
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait(timeout=10)


def test_non_owner_shutdown_preserves_operation_until_owner_process_exits(tmp_path):
    import os, subprocess, sys
    from datetime import datetime, timezone
    from retailq.config import Settings
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.review_contracts import evolve
    from retailq.knowledge.review_store import ReviewStore
    from retailq.knowledge.runtime import KnowledgeRuntime
    from tests.support.reviews import make_operation
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
    store=ReviewStore(root,clock=lambda:datetime.now(timezone.utc),coordinator=coordinator)
    operation=evolve(make_operation(),phase='running');store.put_operation(operation,expected_revision=None)
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),'--marker',str(marker)],env=env)
    try:
        wait_for_owned_phase(marker,child,'maintenance_held',timeout=10)
        owner=SimpleNamespace(root=root,__exit__=lambda *args:None)
        service=SimpleNamespace(repository=KnowledgeRepository(None))
        settings=Settings(_env_file=None,knowledge_data_dir=str(root))
        other=KnowledgeRuntime(service,owner,settings=settings,coordinator=coordinator)
        other.close()
        assert child.poll() is None and coordinator.try_acquire('maintenance') is None
        assert store.get_operation(operation.operation_id).phase=='running'
        child.kill();child.wait(timeout=10)
        replacement=KnowledgeRuntime(service,owner,settings=settings,coordinator=coordinator)
        try:assert store.get_operation(operation.operation_id).phase=='interrupted'
        finally:replacement.close()
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


@pytest.mark.anyio
async def test_scheduler_leader_handover_after_owned_process_exits(tmp_path):
    import os, subprocess, sys
    from datetime import datetime, timedelta, timezone
    from retailq.config import Settings
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.knowledge.scheduler import DailyKnowledgeScheduler
    root=tmp_path/'knowledge';root.mkdir();locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
                            '--marker',str(marker),'--scope','scheduler'],env=env)
    try:
        wait_for_owned_phase(marker,child,'scheduler_held',timeout=10)
        settings=Settings(_env_file=None,knowledge_schedule_enabled=True)
        runtime=SimpleNamespace(root=root,settings=settings,coordinator=coordinator)
        scheduler=DailyKnowledgeScheduler(runtime,clock=lambda:datetime(2026,11,3,12,tzinfo=timezone(timedelta(hours=8))))
        calls=[]
        async def mine():calls.append('run');return {'complete':True}
        scheduler._mine=mine
        await scheduler.run_due();assert calls==[]
        child.kill();child.wait(timeout=10)
        await scheduler.run_due();assert calls==['run']
        await scheduler.run_due();assert calls==['run']
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


@pytest.mark.anyio
async def test_other_process_session_lease_returns_409_before_repository_access(tmp_path):
    import os, subprocess, sys
    from retailq.config import Settings
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.errors import ServiceError
    from retailq.schemas import ChatRequest
    from retailq.services.chat import ChatService
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
                            '--marker',str(marker),'--scope','conversation','--key','42'],env=env)
    class Repository:
        def load_messages(self,*args): raise AssertionError('must not read or write before lease')
        def start_turn(self,*args): raise AssertionError('must not write before lease')
    try:
        wait_for_owned_phase(marker,child,'conversation_held',timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
        service=ChatService(Repository(),object(),Settings(_env_file=None),coordinator=coordinator)
        with pytest.raises(ServiceError) as exc:
            await service.prepare(ChatRequest(message='测试',user_id='test',conversation_id=42))
        assert exc.value.status_code==409 and exc.value.code=='conversation_busy'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


@pytest.mark.anyio
async def test_other_process_session_lease_produces_http_409_without_write(tmp_path):
    import os, subprocess, sys
    import httpx
    from fastapi import FastAPI,Request
    from fastapi.responses import JSONResponse
    from retailq.api.chat import router
    from retailq.config import Settings
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.errors import ServiceError
    from retailq.services.chat import ChatService
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
                            '--marker',str(marker),'--scope','conversation','--key','42'],env=env)
    class Repository:
        def load_messages(self,*args): raise AssertionError('repository access before lease')
        def start_turn(self,*args): raise AssertionError('write before lease')
    try:
        wait_for_owned_phase(marker,child,'conversation_held',timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
        app=FastAPI();app.include_router(router)
        app.state.gateway=object();app.state.chat_service=ChatService(Repository(),object(),Settings(_env_file=None),coordinator=coordinator)
        @app.exception_handler(ServiceError)
        async def handle(request:Request,error:ServiceError):
            return JSONResponse({'error':{'code':error.code}},status_code=error.status_code)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/api/chat/stream',json={'message':'测试','user_id':'test','conversation_id':42})
        assert response.status_code==409 and response.json()['error']['code']=='conversation_busy'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


def test_active_pointer_crash_replays_publication_without_new_generation(tmp_path):
    import os, subprocess, sys
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.knowledge.corpus import CorpusStore
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
                            '--marker',str(marker),'--scenario','publish'],env=env)
    try:
        wait_for_owned_phase(marker,child,'active_pointer_written',timeout=10)
        child.kill();child.wait(timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
        corpus=CorpusStore(root,coordinator)
        assert corpus.active().generation==1
        assert corpus.get_plan('a'*32).phase=='verified'
        assert corpus.publish('a'*32,expected_generation=0).generation==1
        assert corpus.get_plan('a'*32).phase=='published'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)


@pytest.mark.mysql
def test_faith_sql_commit_survives_owned_process_death_without_double_count(tmp_path):
    import os, subprocess, sys
    from uuid import uuid4
    from sqlalchemy import select
    from retailq.config import Settings
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.db import Database
    from retailq.knowledge.faith_repository import FaithEvaluationCase,FaithRepository
    from retailq.knowledge.faith_store import FaithStore
    from retailq.knowledge.rag_models import FaithCase
    if os.environ.get('CH04_FAULT_DB')!='1': pytest.skip('explicit isolated 3309 fault database required')
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    eval_id='Z'+uuid4().hex[:12].upper();run_id=uuid4().hex
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
        '--marker',str(marker),'--scenario','faith_commit','--eval-id',eval_id,'--run-id',run_id],env=env)
    settings=Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'})
    database=Database(settings)
    try:
        wait_for_owned_phase(marker,child,'faith_sql_committed',timeout=20)
        child.kill();child.wait(timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3309,'retailq_test','http://127.0.0.1:19531',root))
        store=FaithStore(root/'faith',coordinator);repository=FaithRepository(database,store);store.resume(run_id)
        case=FaithEvaluationCase(eval_id,'A_policy','故障恢复样例','hybrid_rerank','错误答案','缺少依据',(),'fixture')
        assert repository.record(case,eval_run_id=run_id).seen_count==1
        store.complete(run_id)
        with database.sessions() as session:
            row=session.scalar(select(FaithCase).where(FaithCase.eval_id==eval_id))
            assert row is not None and row.seen_count==1
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        database.close()


@pytest.mark.mysql
def test_corpus_sql_commit_survives_owned_process_death_without_duplicates(tmp_path):
    import os, subprocess, sys
    from uuid import uuid4
    from sqlalchemy import text
    from retailq.config import Settings
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.db import Database
    from retailq.knowledge.corpus import CorpusStore
    from retailq.knowledge.corpus_import import CorpusImporter
    from retailq.knowledge.repository import KnowledgeRepository
    from tests.support.knowledge import TestBudget
    if os.environ.get('CH04_FAULT_DB')!='1': pytest.skip('explicit isolated 3309 fault database required')
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    source=tmp_path/'source';source.mkdir();token=uuid4().hex
    (source/'fixture.md').write_text('# 故障测试\n## '+token+'\n演示规则：未满99元收10元。\n',encoding='utf-8')
    manifest=source/'manifest.json';manifest.write_text(json.dumps({'files':[{'path':'fixture.md','content_type':'policy'}]}),encoding='utf-8')
    build_id=uuid4().hex
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
        '--marker',str(marker),'--scenario','corpus_commit','--run-id',build_id,'--manifest',str(manifest)],env=env)
    settings=Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'})
    database=Database(settings);created=[]
    try:
        ready=wait_for_owned_phase(marker,child,'corpus_sql_committed',timeout=20)
        created=ready['chunk_ids'];assert len(created)==1
        child.kill();child.wait(timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3309,'retailq_test','http://127.0.0.1:19531',root))
        repo=KnowledgeRepository(database);corpus=CorpusStore(root,coordinator)
        assert corpus.get_plan(build_id).phase=='prepared'
        resumed=CorpusImporter(repo,corpus,TestBudget(),collection_prefix='test').resume(build_id)
        assert list(resumed.active_ids)==created and corpus.get_plan(build_id).phase=='sql_committed'
        assert sum(token in row.draft.questions for row in repo.all_chunks())==1
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        # Delete only this invocation's positively identified synthetic row.
        if created:
            with database.engine.begin() as connection:
                for chunk_id in created:
                    connection.execute(text('DELETE FROM knowledge_chunks WHERE id=:id AND questions LIKE :token'),
                                       {'id':chunk_id,'token':'%'+token+'%'})
        database.close()


@pytest.mark.mysql
@pytest.mark.standalone
def test_milvus_upsert_survives_owned_process_death_before_sql_done(tmp_path):
    import os, subprocess, sys
    from threading import Event
    from uuid import uuid4
    from sqlalchemy import text
    from pymilvus import MilvusClient
    from retailq.config import Settings
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.db import Database
    from retailq.knowledge.corpus import CorpusStore
    from retailq.knowledge.corpus_import import CorpusImporter
    from retailq.knowledge.indexing import IndexBuilder
    from retailq.knowledge.milvus_store import MilvusKnowledgeStore
    from retailq.knowledge.repository import KnowledgeRepository
    from tests.support.knowledge import TestBudget
    from tests.support.milvus import require_isolated_milvus
    if os.environ.get('CH04_FAULT_DB')!='1': pytest.skip('explicit isolated 3309 fault database required')
    root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json';env=dict(os.environ)
    source=tmp_path/'source';source.mkdir();token=uuid4().hex
    (source/'fixture.md').write_text('# 故障测试\n## '+token+'\n演示规则：未满99元收10元。\n',encoding='utf-8')
    manifest=source/'manifest.json';manifest.write_text(json.dumps({'files':[{'path':'fixture.md','content_type':'policy'}]}),encoding='utf-8')
    build_id=uuid4().hex;collection='test_'+build_id;uri='http://127.0.0.1:19531'
    require_isolated_milvus(uri,collection)
    probe=MilvusClient(uri)
    try: assert not probe.has_collection(collection)
    finally: probe.close()
    executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
        '--marker',str(marker),'--scenario','index_upsert','--run-id',build_id,'--manifest',str(manifest)],env=env)
    database=Database(Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'}))
    created=[]
    try:
        ready=wait_for_owned_phase(marker,child,'milvus_upserted',timeout=60);created=ready['chunk_ids']
        assert len(created)==1
        child.kill();child.wait(timeout=10)
        coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3309,'retailq_test',uri,root))
        repo=KnowledgeRepository(database);corpus=CorpusStore(root,coordinator)
        assert repo.get_chunks(created)[created[0]].vectorize_status=='pending'
        class NoEmbedding:
            def embed(self,*args,**kwargs): raise AssertionError('matching indexed vector must not be re-embedded')
        importer=CorpusImporter(repo,corpus,TestBudget(),collection_prefix='test')
        builder=IndexBuilder(repo,corpus,importer,coordinator,NoEmbedding(),lambda name:MilvusKnowledgeStore(uri,{'model':'fault-fixture'},collection=name))
        report=builder.retry(build_id,stop=Event())
        assert report.phase=='verified' and report.indexed==1 and report.failed==0
        assert repo.get_chunks(created)[created[0]].vectorize_status=='done'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        client=MilvusClient(uri)
        try:
            require_isolated_milvus(uri,collection)
            if client.has_collection(collection):client.drop_collection(collection)
        finally:client.close()
        if created:
            with database.engine.begin() as connection:
                for chunk_id in created:
                    connection.execute(text('DELETE FROM knowledge_chunks WHERE id=:id AND questions LIKE :token'),{'id':chunk_id,'token':'%'+token+'%'})
        database.close()


@pytest.mark.mysql
@pytest.mark.standalone
def test_old_snapshot_is_searchable_while_another_process_builds_new_generation(tmp_path):
    import os, subprocess, sys
    from threading import Event
    from uuid import uuid4
    from sqlalchemy import text
    from pymilvus import MilvusClient
    from retailq.config import Settings
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.db import Database
    from retailq.knowledge.corpus import CorpusStore
    from retailq.knowledge.corpus_import import CorpusImporter
    from retailq.knowledge.indexing import IndexBuilder
    from retailq.knowledge.milvus_store import MilvusKnowledgeStore
    from retailq.knowledge.repository import KnowledgeRepository
    from tests.support.knowledge import TestBudget
    from tests.support.milvus import require_isolated_milvus
    if os.environ.get('CH04_FAULT_DB')!='1': pytest.skip('explicit isolated 3309 fault database required')
    uri='http://127.0.0.1:19531';root=tmp_path/'knowledge';locks=tmp_path/'locks';marker=tmp_path/'ready.json'
    source=tmp_path/'source';source.mkdir();token=uuid4().hex
    document=source/'fixture.md';manifest=source/'manifest.json'
    document.write_text('# 测试\n## '+token+'\n原版商品型号 OLDLP100 适用体重8公斤。\n',encoding='utf-8')
    manifest.write_text(json.dumps({'files':[{'path':'fixture.md','content_type':'policy'}]}),encoding='utf-8')
    old_id,new_id,third_id=uuid4().hex,uuid4().hex,uuid4().hex;collections=['test_'+old_id,'test_'+new_id,'test_'+third_id]
    probe=MilvusClient(uri)
    try:
        for name in collections:
            require_isolated_milvus(uri,name)
            assert not probe.has_collection(name)
    finally:probe.close()
    database=Database(Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'}))
    coordinator=Coordinator(locks,RuntimeIdentity('127.0.0.1',3309,'retailq_test',uri,root))
    corpus=CorpusStore(root,coordinator);repo=KnowledgeRepository(database)
    importer=CorpusImporter(repo,corpus,TestBudget(),collection_prefix='test')
    class Embedding:
        def embed(self,texts):return [[1.0]+[0.0]*1023 for _ in texts]
    builder=IndexBuilder(repo,corpus,importer,coordinator,Embedding(),lambda name:MilvusKnowledgeStore(uri,{'model':'fault-fixture'},collection=name))
    child=None;created=[]
    try:
        plan=importer.prepare(manifest,operation_id=old_id);created.extend(plan.active_ids)
        assert builder.build(plan,stop=Event()).phase=='verified'
        old=builder.publish(old_id);assert old.generation==1
        document.write_text('# 测试\n## '+token+'\n新版商品型号 NEWLP200 适用体重12公斤。\n',encoding='utf-8')
        env=dict(os.environ);executable=sys._base_executable if os.name=='nt' else sys.executable
        if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
        child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),
            '--marker',str(marker),'--scenario','index_upsert','--run-id',new_id,'--manifest',str(manifest)],env=env)
        ready=wait_for_owned_phase(marker,child,'milvus_upserted',timeout=60);created.extend(ready['chunk_ids'])
        assert corpus.active().generation==1
        old_client=MilvusKnowledgeStore(uri,{'model':'fault-fixture'},collection=old.collection)
        try:
            old_client.ensure_collection()
            assert old_client.bm25_search('OLDLP100',10,None,timeout_seconds=15)[0].id in old.active_ids
        finally:old_client.close()
        child.kill();child.wait(timeout=10)
        assert builder.retry(new_id,stop=Event()).phase=='verified'
        new=builder.publish(new_id);assert new.generation==2 and new.collection!=old.collection
        new_client=MilvusKnowledgeStore(uri,{'model':'fault-fixture'},collection=new.collection)
        try:
            new_client.ensure_collection()
            assert new_client.bm25_search('NEWLP200',10,None,timeout_seconds=15)[0].id in new.active_ids
        finally:new_client.close()
        document.write_text('# 测试\n## '+token+'\n第三版商品型号 THIRDLP300 适用体重15公斤。\n',encoding='utf-8')
        third_plan=importer.prepare(manifest,operation_id=third_id);created.extend(third_plan.active_ids)
        assert set(old.active_ids) <= set(third_plan.retired_ids)
        assert builder.build(third_plan,stop=Event()).phase=='verified'
        assert builder.publish(third_id).generation==3
    finally:
        if child is not None and child.poll() is None:child.kill();child.wait(timeout=10)
        client=MilvusClient(uri)
        try:
            for name in collections:
                require_isolated_milvus(uri,name)
                if client.has_collection(name):client.drop_collection(name)
        finally:client.close()
        if created:
            with database.engine.begin() as connection:
                for chunk_id in set(created):
                    connection.execute(text('DELETE FROM knowledge_chunks WHERE id=:id AND questions LIKE :token'),{'id':chunk_id,'token':'%'+token+'%'})
        database.close()
