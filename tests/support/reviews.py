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
