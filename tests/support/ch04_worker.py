"""Owned child used to observe OS lease behavior without a database mutation."""
import argparse
import json
import os
from pathlib import Path
import time

from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.knowledge.corpus import _write


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True);parser.add_argument('--locks',required=True);parser.add_argument('--marker',required=True)
    parser.add_argument('--scope',choices=('maintenance','conversation','scheduler'),default='maintenance');parser.add_argument('--key',default='')
    parser.add_argument('--scenario',choices=('lease','publish','faith_commit','corpus_commit','index_upsert'),default='lease')
    parser.add_argument('--eval-id');parser.add_argument('--run-id');parser.add_argument('--manifest')
    args=parser.parse_args()
    identity=RuntimeIdentity('127.0.0.1',3309 if args.scenario in ('faith_commit','corpus_commit','index_upsert') else 3307,'retailq_test','http://127.0.0.1:19531',Path(args.root))
    coordinator=Coordinator(Path(args.locks),identity)
    if args.scenario in ('corpus_commit','index_upsert'):
        import re
        from retailq.config import Settings
        from retailq.db import Database
        from retailq.knowledge.corpus import CorpusStore
        from retailq.knowledge.corpus_import import CorpusImporter
        from retailq.knowledge.repository import KnowledgeRepository
        from tests.support.knowledge import TestBudget
        manifest=Path(args.manifest).resolve() if args.manifest else None
        if (os.environ.get('CH04_FAULT_DB')!='1' or not re.fullmatch(r'[0-9a-f]{32}',args.run_id or '')
                or manifest is None or not manifest.is_relative_to(Path(args.root).resolve().parent)):
            raise ValueError('isolated corpus fault test identity required')
        settings=Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'})
        db=Database(settings);repo=KnowledgeRepository(db);original=repo.import_chunks
        try:
            def pause_after_commit(drafts):
                ids=original(drafts)
                _write(Path(args.marker),{'pid':os.getpid(),'phase':'corpus_sql_committed','chunk_ids':ids})
                while True:time.sleep(.1)
            corpus=CorpusStore(Path(args.root),coordinator)
            importer=CorpusImporter(repo,corpus,TestBudget(),collection_prefix='test')
            if args.scenario=='corpus_commit':
                repo.import_chunks=pause_after_commit
                importer.prepare(manifest,operation_id=args.run_id)
            else:
                from threading import Event
                from retailq.knowledge.indexing import IndexBuilder
                from retailq.knowledge.milvus_store import MilvusKnowledgeStore
                from tests.support.milvus import require_isolated_milvus
                uri='http://127.0.0.1:19531';collection='test_'+args.run_id
                require_isolated_milvus(uri,collection)
                plan=importer.prepare(manifest,operation_id=args.run_id)
                class Embedding:
                    def embed(self,texts):return [[1.0]+[0.0]*1023 for _ in texts]
                def store_factory(name):
                    require_isolated_milvus(uri,name)
                    store=MilvusKnowledgeStore(uri,{'model':'fault-fixture'},collection=name)
                    original_upsert=store.upsert
                    def pause_after_upsert(items):
                        result=original_upsert(items)
                        _write(Path(args.marker),{'pid':os.getpid(),'phase':'milvus_upserted','chunk_ids':list(plan.active_ids)})
                        while True:time.sleep(.1)
                    store.upsert=pause_after_upsert
                    return store
                IndexBuilder(repo,corpus,importer,coordinator,Embedding(),store_factory).build(plan,stop=Event())
            return 0
        finally:db.close()
    if args.scenario=='faith_commit':
        import re
        from retailq.config import Settings
        from retailq.db import Database
        from retailq.knowledge.faith_repository import FaithEvaluationCase,FaithRepository
        from retailq.knowledge.faith_store import FaithStore
        if os.environ.get('CH04_FAULT_DB')!='1' or not re.fullmatch(r'Z[0-9A-F]{12}',args.eval_id or '') or not re.fullmatch(r'[0-9a-f]{32}',args.run_id or ''):
            raise ValueError('isolated fault test identity required')
        settings=Settings().model_copy(update={'mysql_host':'127.0.0.1','mysql_port':3309,'mysql_database':'retailq_test'})
        db=Database(settings);store=FaithStore(Path(args.root)/'faith',coordinator);repo=FaithRepository(db,store)
        try:
            store.begin(args.run_id,{'fixture':'faith_commit'})
            case=FaithEvaluationCase(args.eval_id,'A_policy','故障恢复样例','hybrid_rerank','错误答案','缺少依据',(),'fixture')
            repo.record(case,eval_run_id=args.run_id)
            _write(Path(args.marker),{'pid':os.getpid(),'phase':'faith_sql_committed'})
            while True:time.sleep(.1)
        finally:
            store.close();db.close()
    if args.scenario=='publish':
        from dataclasses import replace
        import retailq.knowledge.corpus as module
        corpus=module.CorpusStore(Path(args.root),coordinator)
        plan=module.CorpusPlan('a'*32,0,'test_publish','b'*64,(),(),(),(),'prepared')
        corpus.stage(plan)
        for old,new in [('prepared','sql_committed'),('sql_committed','indexing'),('indexing','verified')]:
            plan=replace(plan,phase=new);corpus.update(plan,expected_phase=old)
        original=module._write
        def pause_after_active(path,value):
            original(path,value)
            if path.name=='active.json':
                _write(Path(args.marker),{'pid':os.getpid(),'phase':'active_pointer_written'})
                while True:time.sleep(.1)
        module._write=pause_after_active
        corpus.publish(plan.build_id,expected_generation=0)
        return 0
    lease=coordinator.try_acquire(args.scope,args.key)
    if lease is None: return 2
    try:
        _write(Path(args.marker),{'pid':os.getpid(),'phase':args.scope+'_held'})
        while True: time.sleep(.1)
    finally:
        lease.close()


if __name__=='__main__': raise SystemExit(main())
