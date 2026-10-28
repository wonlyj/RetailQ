"""Fault injection is confined to this isolated subprocess fixture, never HTTP."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
from threading import Event, Thread
import time
import uuid

from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import DecisionCommand, PrepareCommand, ResumeCommand, SealCommand
from scripts.verify_knowledge_review_recovery import ROOT, test_settings, validate_recovery_scope, write_json


def open_runtime(stack, config):
    from retailq.db import Database
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.vectors import MilvusKnowledgeStore
    from retailq.knowledge.service import KnowledgeService
    from tests.support.knowledge import FakeEmbedding, TestBudget
    from tests.support.reviews import ReviewGateway
    root = Path(config['case']) / 'knowledge'
    settings = test_settings(knowledge_data_dir=str(root), context_window_tokens=31536)
    validate_recovery_scope(settings, root.parent.parent, existing=True)
    database = Database(settings)
    stack.callback(database.close)
    database.ping()
    owner = stack.enter_context(DirectoryOwner(root))
    if config['embedding_mode'] == 'live':
        from retailq.config import Settings
        from retailq.knowledge.embedding import SiliconFlowEmbedding
        from retailq.knowledge.token_budget import BGETokenBudget, TOKENIZER_REVISION, TOKENIZER_SHA256, verify_tokenizer
        live = Settings()
        path = ROOT / 'artifacts/knowledge/assets/tokenizer.json'
        verify_tokenizer(path)
        budget = BGETokenBudget(path)
        embedding = stack.enter_context(SiliconFlowEmbedding(live, budget))
        identity = {'provider':'siliconflow','model':live.embedding_model,'endpoint':live.embedding_base_url.rstrip('/'),
            'dimension':'1024','metric':'COSINE','text_template_version':'1','normalization_version':'l2-v1',
            'tokenizer_revision':TOKENIZER_REVISION,'tokenizer_sha256':TOKENIZER_SHA256,'schema_version':'1'}
    else:
        budget, embedding = TestBudget(), FakeEmbedding()
        identity = {'provider':'test','model':'deterministic-test','endpoint':'fixture',
                    'dimension':'1024','metric':'COSINE','schema_version':'1'}
    vectors = stack.enter_context(MilvusKnowledgeStore(root / 'knowledge.db', identity))
    service = KnowledgeService(KnowledgeRepository(database), embedding, vectors, budget)
    runtime = KnowledgeRuntime(service, owner, settings=settings, embedding=embedding, vectors=vectors,
                               gateway=ReviewGateway(database))
    stack.callback(runtime.close)
    return runtime, database, identity


def execute(runtime, command):
    admission = runtime.admit_review(command)
    if admission.lease:
        result = runtime.execute_review(admission.operation.operation_id, admission.lease, admission.lease.stop)
    else:
        result = admission.operation
    if result.error:
        raise ServiceError(result.error.code, 409, result.error.message)
    return result


def prepare(runtime, database, config):
    from tests.support.reviews import seed_closed_source
    knowledge = runtime.service.repository
    config['baseline_ids'] = [str(r.id) for r in knowledge.all_chunks()]
    answer = f'验收专用规则{config["run_id"]}：擦干后阴凉晾干，避免暴晒。'
    sources = [seed_closed_source(database, f'{config["run_id"]}问法{i}：如何保养？', answer) for i in range(2)]
    config['source_ids'] = [str(s.conversation_id) for s in sources]
    command = PrepareCommand(request_key=config['run_id'], conversation_ids=tuple(s.conversation_id for s in sources))
    admission = runtime.admit_review(command)
    if config['stage'] == 'queued_before_worker':
        return admission, None
    result = runtime.execute_review(command.request_key, admission.lease, Event())
    if result.error:
        raise RuntimeError(result.error.message)
    run = runtime.review_store.get_run(config['run_id'])
    if len(run.group_ids) != 1:
        raise RuntimeError('fixture must produce exactly one two-member group')
    group = runtime.review_store.get_group(run.run_id, run.group_ids[0])
    config['group_id'] = group.group_id
    config['member_ids'] = [str(m.staging_id) for m in group.members]
    command = DecisionCommand(request_key=config['decision_id'], run_id=run.run_id, group_id=group.group_id,
        expected_revision=group.revision, decision=config['decision'],
        category='恢复验收' if config['decision'] == 'approve' else None, reason='本次固定样例人工决定')
    return runtime.admit_review(command), group


def interrupt(runtime, database, config, identity):
    case = Path(config['case'])
    admission, group = prepare(runtime, database, config)
    write_json(case / 'request.json', config)
    stage = config['stage']
    def ready():
        current = runtime.review_store.get_group(config['run_id'], config['group_id']) if group else None
        write_json(case / 'ready.json', {'pid':os.getpid(), 'stage':stage,
            'operation_id':admission.operation.operation_id, 'embedding':identity,
            'planned_id':str(current.application.prepared.chunk_id) if current and current.application and
                current.application.prepared.chunk_id else None,
            'vector_count':int(runtime.vectors.client.get_collection_stats('knowledge')['row_count']),
            'seal_queued':stage == 'seal_active_worker',
            'seal_marker':runtime.review_store.get_seal(config['run_id']) is not None})
        while True:
            time.sleep(.1)  # Parent kills this process; no simulated exception.
    if stage == 'queued_before_worker':
        ready()
    if stage == 'corrupt_run_seal':
        broken = runtime.root / 'reviews/runs' / config['run_id'] / 'run.json'
        broken.write_bytes(b'{broken-review-run')
        seal = SealCommand(request_key=config['seal_id'], run_id=config['run_id'], confirm_run_id=config['run_id'])
        runtime.queue_review_seal(seal)
        admission.lease.close()
        execute(runtime, seal)
        ready()
    original_put = runtime.review_store.put_group
    def put_group(record, **kwargs):
        if stage == 'after_sql_commit_before_applied' and record.application and record.application.phase == 'applied':
            ready()
        saved = original_put(record, **kwargs)
        if stage == 'after_decision_persisted' and saved.decision and saved.application is None:
            ready()
        return saved
    runtime.review_store.put_group = put_group
    original_apply = runtime.review_repository.apply_group
    def apply_group(record, decision, prepared):
        def on_prepared(value):
            prepared(value)
            if stage == 'after_prepared_before_sql_commit':
                ready()
        if stage == 'seal_active_worker':
            # Hold the actual admitted worker before its SQL boundary; queue the
            # seal on another thread while this lease remains active.
            seal = SealCommand(request_key=config['seal_id'], run_id=config['run_id'], confirm_run_id=config['run_id'])
            task = Thread(target=runtime.queue_review_seal, args=(seal,))
            task.start()
            task.join(timeout=10)
            if task.is_alive():
                raise RuntimeError('seal queue failed to reach safe wait boundary')
            ready()
        return original_apply(record, decision, on_prepared)
    runtime.review_repository.apply_group = apply_group
    if stage == 'after_milvus_upsert_before_mysql_done':
        original_vectorize = runtime.service.vectorize_pending
        def vectorize(**kwargs):
            def fault(point):
                if point == 'after_milvus_upsert':
                    ready()
            return original_vectorize(**kwargs, fault=fault)
        runtime.service.vectorize_pending = vectorize
    result = runtime.execute_review(admission.operation.operation_id, admission.lease, admission.lease.stop)
    raise RuntimeError(f'worker missed requested checkpoint: {result.phase}')


def recover(runtime, database, config, identity):
    from retailq.knowledge.text import content_fingerprint
    stage, rid = config['stage'], config['run_id']
    sealed = stage in ('corrupt_run_seal', 'seal_active_worker')
    revival_blocked = False
    if sealed:
        seal = SealCommand(request_key=config['seal_id'], run_id=rid, confirm_run_id=rid)
        runtime.queue_review_seal(seal)
        execute(runtime, seal)
        try:
            execute(runtime, ResumeCommand(request_key=uuid.uuid4().hex, run_id=rid))
        except ServiceError as exc:
            revival_blocked = exc.code == 'knowledge_review_sealed'
        phase = 'sealed'
    else:
        execute(runtime, ResumeCommand(request_key=uuid.uuid4().hex, run_id=rid))
        phase = runtime.review_store.get_run(rid).phase
    knowledge = runtime.service.repository
    rows = [r for r in knowledge.all_chunks() if str(r.id) not in config['baseline_ids']]
    result = {'pid':os.getpid(), 'phase':phase, 'sealed':runtime.review_store.get_seal(rid) is not None,
        'revival_blocked':revival_blocked, 'embedding':identity, 'new_chunk_count':len(rows),
        'new_chunk_ids':[str(r.id) for r in rows], 'pending_count':sum(r.vectorize_status == 'pending' for r in rows),
        'vector_count':int(runtime.vectors.client.get_collection_stats('knowledge')['row_count'])}
    if sealed or stage == 'queued_before_worker':
        raw_preserved = stage != 'corrupt_run_seal' or (runtime.root / 'reviews/runs' / rid / 'run.json').read_bytes() == b'{broken-review-run'
        result['passed'] = not rows and result['vector_count'] == 0 and raw_preserved and (
            result['sealed'] and revival_blocked if sealed else phase == 'waiting_review')
    else:
        group = runtime.review_store.get_group(rid, config['group_id'])
        confirmed = group.application.phase == 'applied' and runtime.review_repository.inspect_application(group) == 'committed'
        target = group.application.prepared.chunk_id
        prepared = group.application.prepared
        staged = {r.id:r for r in knowledge.staging(tuple({m.batch_no for m in group.members}))}
        match = all(str(m.staging_id) in config['member_ids'] and staged[m.staging_id].status == m.status
                    for m in prepared.members) and len(prepared.members) == len(config['member_ids'])
        fingerprints = runtime.vectors.fingerprints([target]) if target else {}
        correct_fingerprint = target is None or (len(rows) == 1 and rows[0].id == target and
            content_fingerprint(rows[0].draft) == prepared.fingerprint == fingerprints.get(target) and
            rows[0].vector_id == str(target) and rows[0].vectorize_status == 'done')
        result.update(confirmed=confirmed, all_members_match=match, fingerprint_matches=correct_fingerprint,
            target_id=str(target) if target else None, dispositions=[m.model_dump(mode='json') for m in prepared.members],
            fingerprint=prepared.fingerprint, operation=runtime.review_store.get_operation(config['decision_id']).model_dump(mode='json'))
        expected = 1 if config['decision'] == 'approve' else 0
        result['passed'] = confirmed and match and correct_fingerprint and len(rows) == expected and result['vector_count'] == expected and phase == 'completed'
    write_json(Path(config['case']) / 'recovered.json', result)
    return 0 if result['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    parser.add_argument('--mode', choices=['interrupt', 'recover'], required=True)
    args = parser.parse_args(argv)
    request = Path(args.request).resolve()
    config = json.loads(request.read_text(encoding='utf-8'))
    case = Path(config['case']).resolve()
    validate_recovery_scope(test_settings(), case.parent, existing=True)
    if request != case / 'request.json' or not case.is_relative_to(case.parent) or case.parent == case:
        raise ValueError('unsafe worker IPC scope')
    with ExitStack() as stack:
        runtime, database, identity = open_runtime(stack, config)
        if args.mode == 'interrupt':
            interrupt(runtime, database, config, identity)
        return recover(runtime, database, config, identity)


if __name__ == '__main__':
    raise SystemExit(main())
