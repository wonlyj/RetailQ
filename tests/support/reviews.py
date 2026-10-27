"""Fixtures for real review records; external model doubles are added as needed."""
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import hashlib
import pytest

from retailq.knowledge.contracts import ConversationSnapshot, MiningMessage

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone(timedelta(hours=8)))
RID = 'a' * 32


def make_store(root):
    from retailq.knowledge.review_store import ReviewStore
    return ReviewStore(root, clock=lambda: NOW)


def make_source():
    return ConversationSnapshot('conversation:1:message:2', 1, 2,
                                (MiningMessage(1, 'user', '怎么清洁？'),
                                 MiningMessage(2, 'assistant', '擦干后阴凉晾干。')))


def make_run(run_id=RID):
    from retailq.knowledge.review_contracts import RunRecord
    return RunRecord(run_id=run_id, rule_hash='c' * 64, sources=(make_source(),),
                     created_at=NOW.isoformat(), updated_at=NOW.isoformat())


def make_operation(key=RID, ids=(1,)):
    from retailq.knowledge.review_contracts import (
        OperationRecord, PrepareCommand, SelectionSummary, canonical_request_hash,
    )
    command = PrepareCommand(request_key=key, conversation_ids=ids)
    return OperationRecord(operation_id=key, run_id=key, kind='prepare',
                           request_hash=canonical_request_hash(command), command=command,
                           source_snapshot=(make_source(),),
                           selection=SelectionSummary(eligible_total=1, selected=1, remaining=0),
                           rule_hash='c' * 64, accepted_at=NOW.isoformat())


@dataclass
class ReviewHarness:
    database: object
    knowledge: object
    repository: object
    store: object
    budget: object
    embedding: object
    vectors: object


@pytest.fixture
def review_harness(test_database, tmp_path):
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.review_repository import ReviewRepository
    from retailq.knowledge.vectors import MilvusKnowledgeStore
    from tests.support.knowledge import TestBudget, FakeEmbedding
    from tests.test_knowledge_vectors import identity
    knowledge = KnowledgeRepository(test_database)
    with MilvusKnowledgeStore(tmp_path / 'knowledge.db', identity()) as vectors:
        yield ReviewHarness(test_database, knowledge, ReviewRepository(knowledge),
                            make_store(tmp_path), TestBudget(), FakeEmbedding(), vectors)


def seed_closed_source(database, question='怎么清洁？', answer='擦干后阴凉晾干。'):
    from retailq.models import Conversation, Message
    with database.sessions.begin() as session:
        source = Conversation(user_id='review-local-test', status='已结束')
        session.add(source)
        session.flush()
        messages = [Message(conversation_id=source.id, role='user', content=question),
                    Message(conversation_id=source.id, role='assistant', content=answer)]
        session.add_all(messages)
        session.flush()
        return ConversationSnapshot(f'conversation:{source.id}:message:{messages[-1].id}',
                                    source.id, messages[-1].id,
                                    tuple(MiningMessage(m.id, m.role, m.content) for m in messages))


def vector_count(h):
    return int(h.vectors.client.get_collection_stats('knowledge')['row_count'])


def seed_new_group(h, count=2):
    from retailq.knowledge.contracts import QAProposal
    from retailq.knowledge.review_contracts import ReviewGroup, SourceQA
    sources = [seed_closed_source(h.database, question=f'问法{i}：怎么清洁？') for i in range(count)]
    batch = hashlib.sha256(str(sources[0].conversation_id).encode()).hexdigest()
    rows = h.knowledge.stage(batch, [QAProposal(s.source_ref, s.messages[0].content,
                                               s.messages[-1].content, s.messages[-1].content) for s in sources])
    members = tuple(SourceQA(staging_id=row.id, batch_no=row.batch_no, source_ref=row.source_ref,
                             question=row.question, answer=row.answer,
                             message_ids=tuple(m.id for m in source.messages))
                    for row, source in zip(rows, sources))
    return ReviewGroup(run_id=RID, group_id='b'*32, members=members,
                        representative_id=rows[0].id, questions=tuple(r.question for r in rows),
                        answer=rows[0].answer)


def approve(category='商品使用与保养'):
    from retailq.knowledge.review_contracts import ReviewDecision
    return ReviewDecision(operation_id='d'*32, kind='approved', category=category, decided_at=NOW.isoformat())


def reject():
    from retailq.knowledge.review_contracts import ReviewDecision
    return ReviewDecision(operation_id='d'*32, kind='rejected', decided_at=NOW.isoformat())


def group_batch_ids(group):
    return list(dict.fromkeys(m.batch_no for m in group.members))


class ReviewGateway:
    """Only model boundaries are deterministic; records/SQL/Lite remain real."""
    def __init__(self, database=None):
        self.database = database
        self.extracted_refs = []
        self.classified_ids = []

    def extract(self, batch):
        from retailq.knowledge.contracts import QAProposal
        if self.database:
            assert self.database.engine.pool.checkedout() == 0
        self.extracted_refs.extend(s.source_ref for s in batch)
        return [QAProposal(s.source_ref, s.messages[0].content, s.messages[-1].content,
                           s.messages[-1].content) for s in batch]

    def deduplicate(self, candidates):
        from tests.test_knowledge_mining import Gateway
        return Gateway().deduplicate(candidates)

    def classify(self, candidates):
        from retailq.knowledge.review_contracts import CategorySuggestion
        self.classified_ids.extend(c.group_id for c in candidates)
        return [CategorySuggestion(group_id=c.group_id, category='商品使用与保养',
                                   reason='测试样例建议，需人工审核。') for c in candidates]

    def close(self):
        pass


def make_preparer(h, gateway=None, **kwargs):
    from retailq.knowledge.review_preparation import ReviewPreparation
    return ReviewPreparation(h.knowledge, h.repository, h.store,
                              gateway or ReviewGateway(h.database), h.embedding,
                              input_budget=30000, **kwargs)


def queued_run_with_sources(h, sources, preparer=None):
    from retailq.knowledge.review_contracts import (
        PrepareCommand, OperationRecord, SelectionSummary, RunRecord, canonical_request_hash,
    )
    preparer = preparer or make_preparer(h)
    command = PrepareCommand(request_key=RID, conversation_ids=tuple(s.conversation_id for s in sources))
    h.store.put_operation(OperationRecord(operation_id=RID, run_id=RID, kind='prepare', command=command,
                          request_hash=canonical_request_hash(command), source_snapshot=tuple(sources),
                          selection=SelectionSummary(eligible_total=len(sources), selected=len(sources), remaining=0),
                          rule_hash=preparer.rule_hash(), accepted_at=NOW.isoformat()), expected_revision=None)
    return h.store.put_run(RunRecord(run_id=RID, rule_hash=preparer.rule_hash(), sources=tuple(sources),
                           created_at=NOW.isoformat(), updated_at=NOW.isoformat()), expected_revision=None)


def make_application(h):
    from retailq.knowledge.service import KnowledgeService
    from retailq.knowledge.review_application import ReviewApplication
    if not hasattr(h, 'service'):
        h.service = KnowledgeService(h.knowledge, h.embedding, h.vectors, h.budget)
    if not hasattr(h, 'application'):
        h.application = ReviewApplication(h.knowledge, h.repository, h.store, h.service)
    return h.application


def prepared_new_group(h, suggested='商品使用与保养'):
    from threading import Event
    from retailq.knowledge.review_contracts import CategorySuggestion, evolve
    sources = [seed_closed_source(h.database, '怎么清洁？'), seed_closed_source(h.database, '清洁有什么要求？')]
    queued_run_with_sources(h, sources)
    run = make_preparer(h).prepare(RID, stop=Event())
    gid = run.group_ids[0]
    group = h.store.get_group(RID, gid)
    if group.suggestion.category != suggested:
        h.store.put_group(evolve(group, suggestion=CategorySuggestion(group_id=gid, category=suggested,
                          reason='样例建议')), expected_revision=group.revision)
    make_application(h)
    return RID, gid


def queued_decision(h, rid, gid, *, decision='approve', category='售后手册', key='d'*32,
                    expected_revision=None, reason=None):
    from retailq.knowledge.review_contracts import DecisionCommand, OperationRecord, canonical_request_hash
    group = h.store.get_group(rid, gid)
    command = DecisionCommand(request_key=key, run_id=rid, group_id=gid,
        expected_revision=group.revision if expected_revision is None else expected_revision,
        decision=decision, category=category, reason=reason)
    op = OperationRecord(operation_id=key, run_id=rid, kind='decide', request_hash=canonical_request_hash(command),
                          command=command, accepted_at=NOW.isoformat())
    return h.store.put_operation(op, expected_revision=None)


queued_approval = queued_decision


def confirmed_target(h, rid, gid):
    group = h.store.get_group(rid, gid)
    assert group.application.phase == 'applied'
    assert h.repository.inspect_application(group) == 'committed'
    return group.application.prepared.chunk_id


def queued_seal(h, rid=RID, key='e'*32):
    from retailq.knowledge.review_contracts import SealCommand, OperationRecord, canonical_request_hash
    command = SealCommand(request_key=key, run_id=rid, confirm_run_id=rid, reason='人工终止')
    return h.store.put_operation(OperationRecord(operation_id=key, run_id=rid, kind='seal',
                                request_hash=canonical_request_hash(command), command=command,
                                accepted_at=NOW.isoformat()), expected_revision=None)


def make_queries(h):
    from retailq.knowledge.review_queries import ReviewQueries
    return ReviewQueries(h.repository, h.knowledge, h.store, h.store.root / 'runs')


def make_review_runtime(h, *, gateway=None):
    from retailq.config import Settings
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    make_application(h)
    settings = Settings(_env_file=None, knowledge_enabled=True, knowledge_schedule_enabled=False,
                        knowledge_data_dir=str(h.store.root), context_window_tokens=31536)
    return KnowledgeRuntime(h.service, DirectoryOwner(h.store.root).__enter__(), settings=settings,
                             embedding=h.embedding, vectors=h.vectors, gateway=gateway)


@pytest.fixture
async def job_env(review_harness):
    import asyncio
    from threading import Event
    from types import SimpleNamespace
    from retailq.knowledge.review_jobs import ReviewJobs
    from retailq.knowledge.review_contracts import PrepareCommand
    h = review_harness
    entered, release, exited = Event(), Event(), Event()
    class Controlled(ReviewGateway):
        def extract(self, batch):
            entered.set()
            try:
                if not release.wait(10):
                    raise TimeoutError('test must release the external model boundary')
                return super().extract(batch)
            finally:
                exited.set()
    gateway = Controlled(h.database)
    source = seed_closed_source(h.database)
    runtime = make_review_runtime(h, gateway=gateway)
    jobs = ReviewJobs(runtime)
    command = PrepareCommand(request_key=RID, conversation_ids=(source.conversation_id,))
    env = SimpleNamespace(h=h, runtime=runtime, jobs=jobs, entered=entered, release=release,
                          exited=exited, gateway=gateway, source=source, command=command)
    try:
        yield env
    finally:
        release.set()
        await jobs.aclose()
        await asyncio.to_thread(runtime.close)


def review_runtime_factory(settings, database):
    """CLI dependency injection: real runtime/storage, only external models fake."""
    from pathlib import Path
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.vectors import MilvusKnowledgeStore
    from retailq.knowledge.service import KnowledgeService
    from tests.support.knowledge import FakeEmbedding, TestBudget
    from tests.test_knowledge_vectors import identity
    owner = DirectoryOwner(Path(settings.knowledge_data_dir)).__enter__()
    vectors = MilvusKnowledgeStore(owner.root / 'knowledge.db', identity())
    embedding = FakeEmbedding()
    service = KnowledgeService(KnowledgeRepository(database), embedding, vectors, TestBudget())
    return KnowledgeRuntime(service, owner, settings=settings, embedding=embedding, vectors=vectors,
                             gateway=ReviewGateway(database))
