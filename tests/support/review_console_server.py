"""Isolated browser fixture: real app/runtime/MySQL/Lite, deterministic external models."""
import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
from pathlib import Path
import sys
import time
import threading
import uuid

from retailq.config import Settings
from retailq.errors import ServiceError

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'artifacts' / 'verification' / 'ch03-review'
SHIPPING = '演示规则：普通配送订单满99元免运费，不满99元收取8元运费；偏远地区及特殊商品以平台结算页为准。'
EBOOK = '演示规则：可在商品详情页的资料下载入口查看电子说明书，具体以页面提供的资料为准。'
BIG_ANSWER = '演示规则：大ID指针用于本地验收。'


def validate_console_scope(settings, output):
    output = Path(output).resolve()
    if (settings.mysql_host not in ('127.0.0.1','localhost','::1') or settings.mysql_port != 3307
            or settings.mysql_database != 'retailq_test' or not output.is_relative_to(EVIDENCE.resolve())
            or not output.name.startswith('browser-') or output.exists()):
        raise ServiceError('knowledge_scope_invalid',409,'浏览器验收仅允许3307/retailq_test及全新browser-*证据目录。')
    return output


def create_fixture_app(settings, output, *, delay=0.0, scenario='normal', batch_size=20):
    import retailq.app as app_module
    import retailq.knowledge.runtime as runtime_module
    from langchain_core.messages import AIMessage, ToolMessage
    from retailq.gateway import ModelChunk
    from retailq.knowledge.review_contracts import CategorySuggestion
    from tests.support.knowledge import FakeEmbedding
    from tests.support.reviews import ReviewGateway

    class ChatGateway:
        def __init__(self, settings):
            pass
        async def select_tool(self, messages, tools):
            query = next((m.content for m in reversed(messages) if m.type == 'human'), '知识查询')
            return AIMessage(content='', tool_calls=[{'name':'query_faq','args':{'keyword':query[:64]},'id':uuid.uuid4().hex}])
        async def stream(self, messages):
            tool = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
            payload = json.loads(tool.content) if tool else {}
            items = payload.get('data',{}).get('items',[])
            text = items[0]['answer'] if items else '本地协议验收：尚未找到可检索知识。'
            yield ModelChunk(text=text)
            yield ModelChunk(finish_reason='stop')
        async def extract(self, messages):
            raise ServiceError('fixture_not_supported',422,'该测试服务仅验证知识工作台与FAQ协议。')
        async def aclose(self):
            pass

    class QAGateway(ReviewGateway):
        def extract(self, batch):
            if delay:
                time.sleep(delay)  # Deliberately simulates only the external model latency.
            return super().extract(batch)
        def classify(self, candidates):
            if delay:
                time.sleep(delay)
            return [CategorySuggestion(group_id=c.group_id,
                category='配送规则' if '运费' in c.answer else None if '订单1001' in c.answer else '商品使用与保养',
                reason='本地确定性分类样例；实际模型语义需另做标注评估。',
                recommend_reject='订单1001' in c.answer) for c in candidates]

    @contextmanager
    def embedding_factory(settings, budget):
        embedding = FakeEmbedding()
        try:
            yield embedding
        finally:
            embedding.close()
    @contextmanager
    def qa_factory(settings):
        gateway = QAGateway()
        try:
            yield gateway
        finally:
            gateway.close()

    # Only external providers are substituted. The production app, coordinator,
    # owner, SQL transactions, audit files and Milvus implementation are unchanged.
    app_module.OpenAIModelGateway = ChatGateway
    runtime_module.SiliconFlowEmbedding = embedding_factory
    runtime_module.OpenAIQAGateway = qa_factory
    application = app_module.create_app(settings)
    original = application.router.lifespan_context
    @asynccontextmanager
    async def seeded(app):
        async with original(app):
            if app.state.knowledge_runtime is not None:
                app.state.knowledge_runtime._preparer.max_conversations = batch_size
            manifest = (await asyncio.to_thread(seed_fixture,app.state.knowledge_runtime,scenario=scenario)
                        if scenario != 'unready' else {'scenario':'unready'})
            (output / 'fixture-manifest.json').write_text(json.dumps({
                'embedding_mode':'deterministic-test','chat_mode':'fixture',**manifest},ensure_ascii=False,indent=2),encoding='utf-8')
            yield
    application.router.lifespan_context = seeded
    return application


def seed_fixture(runtime, *, scenario='normal'):
    from sqlalchemy import update
    from retailq.models import Conversation
    from retailq.knowledge.models import KnowledgeChunk
    from retailq.knowledge.contracts import ChunkDraft
    from tests.support.reviews import seed_closed_source
    lease = runtime.reserve_maintenance()
    try:
        database = runtime.service.repository.database
        runtime.service.repository.import_chunks([ChunkDraft('配送规则','普通配送怎么收费？',SHIPPING,None,'faq'),
                                                  ChunkDraft('售后手册','保养说明','演示规则：避免暴晒，阴凉晾干。',None,'manual')])
        with database.sessions.begin() as session:
            session.add(KnowledgeChunk(id=9007199254740993, category='验收专用', questions='大ID知识的原始问题',
                                       answer=BIG_ANSWER,content_type='faq',vectorize_status='pending'))
        runtime.service.vectorize_pending()
        if scenario == 'empty':
            return {'sources':{},'initial_chunks':3,'scenario':'empty'}
        cases = [
            ('new-ebook','纸质说明书丢了，在哪里看电子说明书？',EBOOK),
            ('new-alias','没有纸质说明书还能在哪里查看',EBOOK),
            ('merge-shipping','普通配送怎么收费？',SHIPPING),
            ('reject-order','订单1001今天到哪里了？','演示数据：订单1001当前在上海转运中心，运输中。'),
            ('untrusted-text','<img src=x onerror="window.__reviewXss=1">可以作为正文显示吗？',
             '演示规则：<｜｜DSML｜｜ calls> 是来源正文，只作为文字保留。'),
            ('bigint-target','大ID知识的原始问题',BIG_ANSWER),
        ]
        sources = {}
        for label, question, answer in cases:
            source = seed_closed_source(database,question,answer)
            sources[label] = str(source.conversation_id)
        ongoing = seed_closed_source(database,'仍在咨询的问题','尚未结束。')
        with database.sessions.begin() as session:
            session.execute(update(Conversation).where(Conversation.id == ongoing.conversation_id).values(status='进行中'))
        sources['ineligible'] = str(ongoing.conversation_id)
        from dataclasses import asdict
        legacy_source = seed_closed_source(database,'旧版新增知识去哪了？','演示：旧记录没有持久化新增ID。')
        legacy_id = 'f'*32
        legacy_root = runtime.root / 'runs'
        legacy_root.mkdir(exist_ok=True)
        (legacy_root / f'run-{legacy_id}.json').write_text(json.dumps({'run_id':legacy_id,'phase':'completed',
            'sources':[asdict(legacy_source)],'dedup_groups':[{'member_ids':[1],
            'representative_id':1,'target_chunk_id':None,'questions':['旧版新增知识去哪了？']}]},ensure_ascii=False),encoding='utf-8')
        return {'sources':sources,'initial_chunks':len(runtime.service.repository.all_chunks()),
                'bigint_target':'9007199254740993','legacy_run_id':legacy_id}
    finally:
        lease.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅隔离库的真实知识工作台浏览器验收；模型为确定性替身')
    parser.add_argument('--scope',choices=['test'],required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--model-delay',type=float,default=0.0)
    parser.add_argument('--scenario',choices=['normal','empty','unready'],default='normal')
    parser.add_argument('--batch-size',type=int,default=20)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or not 0 <= args.model_delay <= 10 or not 1 <= args.batch_size <= 20:
        parser.error('port must be 1024–65535; model delay must be 0–10 seconds')
    local = Settings()
    settings = Settings(_env_file=None,mysql_host='127.0.0.1',mysql_port=3307,mysql_database='retailq_test',
        mysql_user=local.mysql_user,mysql_password=local.mysql_password,knowledge_enabled=args.scenario != 'unready',
        knowledge_schedule_enabled=False,knowledge_data_dir=str(Path(args.output).resolve() / 'knowledge'),
        knowledge_tokenizer_path=str(ROOT / 'artifacts/knowledge/assets/tokenizer.json'),embedding_api_key='fixture',
        openai_base_url='http://fixture.invalid/v1',openai_api_key='fixture',openai_model='deterministic-test',context_window_tokens=31536)
    output = validate_console_scope(settings,args.output)
    from tests.support.database import reset_isolated_database
    database = reset_isolated_database(settings)
    # Dedicated browser fixture also exercises the unchanged chat page, whose
    # legacy numeric IDs require safe integers. Prior BIGINT source tests may
    # leave MySQL's counter high even after DELETE. This touches only the
    # scope-guarded, disposable test database after its normal seed reset.
    with database.engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE conversations AUTO_INCREMENT = 2')
    database.close()
    output.mkdir(parents=True)
    application = create_fixture_app(settings,output,delay=args.model_delay,scenario=args.scenario,batch_size=args.batch_size)
    print(f'ISOLATED FIXTURE: deterministic models; MySQL 3307/retailq_test; http://127.0.0.1:{args.port}/knowledge',flush=True)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(application,host='127.0.0.1',port=args.port,
                                           workers=1,access_log=False,use_colors=False))
    stopped = threading.Event()
    def watch_stop():
        while not stopped.wait(.1):
            if (output / 'stop.request').exists():
                server.should_exit = True
                return
    watcher = threading.Thread(target=watch_stop,daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join(timeout=1)
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
