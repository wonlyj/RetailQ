"""Explicit offline maintenance. Never opens a second owner beside the app."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading

from pydantic import ValidationError

from retailq.config import Settings
from retailq.db import Database
from retailq.errors import ServiceError


def prepare_tokenizer(path):
    import httpx
    from retailq.knowledge.token_budget import TOKENIZER_REVISION,TOKENIZER_SHA256,verify_tokenizer
    path=Path(path)
    if path.exists():verify_tokenizer(path)
    else:
        url=f'https://huggingface.co/BAAI/bge-m3/resolve/{TOKENIZER_REVISION}/tokenizer.json'
        with httpx.Client(trust_env=False,follow_redirects=True,timeout=30) as client:
            response=client.get(url);response.raise_for_status();content=response.content
        if hashlib.sha256(content).hexdigest()!=TOKENIZER_SHA256:raise ValueError('BGE tokenizer checksum mismatch')
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.json.tmp');temporary.write_bytes(content);temporary.replace(path)
    return {'tokenizer':str(path),'sha256':TOKENIZER_SHA256}


def parser():
    common=argparse.ArgumentParser(add_help=False)
    common.add_argument('--data-dir',default=argparse.SUPPRESS)
    common.add_argument('--output',default=argparse.SUPPRESS)
    root=argparse.ArgumentParser(description='RetailQ知识维护',parents=[common])
    sub=root.add_subparsers(dest='action',required=True)
    for name in ['init-db','prepare-tokenizer','import','import-faq','vectorize','reconcile','mine','status','query','prepare-corpus','build-index','publish-index']:
        command=sub.add_parser(name,parents=[common])
        if name=='init-db':
            command.add_argument('--scope',choices=['demo','test'],required=True)
            command.add_argument('--chapter',choices=['3','4'],default='3')
        if name in ('import','prepare-corpus'):
            command.add_argument('--manifest',required=True)
            command.add_argument('--build-id')
        if name in ('build-index','publish-index'):command.add_argument('--build-id',required=True)
        if name=='mine':command.add_argument('--resume')
        if name=='query':
            command.add_argument('--text',required=True)
            command.add_argument('--strategy',choices=['dense','bm25','hybrid','hybrid_rerank'],default='hybrid_rerank')
            command.add_argument('--category')
    return root


def main(argv=None,*,settings=None,runtime_factory=None):
    args=parser().parse_args(argv)
    database=runtime=None
    code=0
    try:
        settings=settings or Settings()
        overrides={'knowledge_enabled':True}
        if getattr(args,'data_dir',None):
            overrides['knowledge_data_dir']=args.data_dir
            if settings.knowledge_tokenizer_path==Settings.model_fields['knowledge_tokenizer_path'].default:
                overrides['knowledge_tokenizer_path']=str(Path(args.data_dir)/'assets/tokenizer.json')
        settings=settings.model_copy(update=overrides)
        if args.action=='prepare-tokenizer':
            if settings.is_database_configured:
                from retailq.coordination import Coordinator
                lease=Coordinator.for_settings(settings).try_acquire('maintenance')
                if lease is None:raise ServiceError('knowledge_job_busy',409,'另一个知识维护任务正在执行。')
                with lease:result=prepare_tokenizer(settings.knowledge_tokenizer_path)
            else:
                result=prepare_tokenizer(settings.knowledge_tokenizer_path)
        else:
            if not settings.is_database_configured:raise ServiceError('database_not_configured',503,'请先配置本地MySQL。')
            if args.action=='init-db':
                expected=('retailq_test',3307) if args.scope=='test' else ('retailq',3306)
                if (settings.mysql_database,settings.mysql_port)!=expected or settings.mysql_host not in ('127.0.0.1','localhost'):
                    raise ServiceError('knowledge_scope_invalid',409,'数据库范围与scope不匹配，未迁移。')
                database=Database(settings)
                from scripts.migrate_knowledge import migrate
                migrate(database,Path(__file__).resolve().parents[1]/'db/init/03-knowledge.sql')
                if args.chapter=='4':
                    from scripts.migrate_rag import migrate_rag
                    migrate_rag(database,Path(__file__).resolve().parents[1]/'db/init/04-rag.sql')
                result={'migrated':True,'scope':args.scope,'chapter':args.chapter}
            elif args.action=='status':
                database=Database(settings)
                from retailq.knowledge.repository import KnowledgeRepository
                rows=KnowledgeRepository(database).all_chunks()
                result={'chunks':len(rows),'pending':sum(r.vectorize_status=='pending' for r in rows),'done':sum(r.vectorize_status=='done' for r in rows),'active':0,'history':len(rows),'active_pending':0,'vector_verification':'not_checked'}
                root=Path(settings.knowledge_data_dir)
                if not root.is_absolute():root=Path(__file__).resolve().parents[1]/root
                if (root/'corpus/active.json').exists():
                    from retailq.coordination import Coordinator
                    from retailq.knowledge.corpus import CorpusStore,is_active
                    snapshot=CorpusStore(root,Coordinator.for_settings(settings)).active()
                    result.update(active=sum(is_active(row,snapshot) for row in rows),history=sum(not is_active(row,snapshot) for row in rows),
                                  active_pending=sum(is_active(row,snapshot) and row.vectorize_status!='done' for row in rows))
            else:
                if not settings.is_embedding_configured:raise ServiceError('embedding_not_configured',503,'请先配置独立EMBEDDING_API_KEY。')
                database=Database(settings)
                if runtime_factory is None:
                    from retailq.knowledge.runtime import KnowledgeRuntime
                    runtime_factory=KnowledgeRuntime.open
                runtime=runtime_factory(settings,database)
                if args.action=='query':
                    if hasattr(runtime,'corpus'):
                        retrieved=runtime.retrieve(args.text,strategy=args.strategy,category=args.category)
                        items=[{'id':str(c.id),'question':c.draft.questions,'answer':c.draft.answer,'category':c.draft.category,
                                'rank':c.rank,'score':c.score} for c in retrieved.candidates]
                    else:items=runtime.search_faq(args.text)
                    result={'found':bool(items),'items':items,'strategy':args.strategy,'mode':'diagnostic'}
                else:
                    action='import' if args.action=='import-faq' else args.action
                    options={'faq':True} if args.action=='import-faq' else vars(args)
                    result=runtime.maintain(action,options,threading.Event())
                    if action=='vectorize' and (result['failed'] or result['pending_after']):code=1
                    if action=='mine' and not result['complete'] and result.get('phase')!='waiting_review':code=1
    except (ServiceError,ValidationError,ImportError,ValueError,OSError) as exc:
        result={'error':{'code':exc.code if isinstance(exc,ServiceError) else 'knowledge_configuration_invalid',
            'message':exc.public_message if isinstance(exc,ServiceError) else '知识维护未完成，请检查本地配置、依赖及输入。'}}
        code=2
    except Exception:
        result={'error':{'code':'knowledge_execution_failed','message':'知识维护执行未成功，请检查本地运行状态。'}};code=1
    finally:
        cleanup_failed=False
        try:
            if runtime is not None:runtime.close()
        except Exception:cleanup_failed=True
        try:
            if database is not None:database.close()
        except Exception:cleanup_failed=True
        if cleanup_failed:
            result={'error':{'code':'knowledge_close_failed','message':'知识资源关闭未完全确认，请核查本地进程。'}};code=1
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if getattr(args,'output',None):
        target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(output+'\n',encoding='utf-8')
    print(output)
    return code


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
