"""Catch duplicate publication, unauthorized changes and false recovery confirmations."""
from threading import Event

import pytest
from sqlalchemy import select

from retailq.errors import ServiceError
from retailq.models import Message
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.models import KnowledgeChunk
from retailq.knowledge.review_contracts import evolve
from retailq.knowledge.text import embedding_text
from tests.support.reviews import (
    RID, review_harness, prepared_new_group, queued_approval, queued_decision, confirmed_target,
    make_application, queued_seal, vector_count, seed_closed_source, queued_run_with_sources, make_preparer,
)

pytestmark = [pytest.mark.mysql, pytest.mark.lite]


def test_approval_uses_human_category_and_same_id_on_retry(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid, category='售后手册')
    recorded_texts = []
    original = h.embedding.embed
    def observed(texts, **kwargs):
        assert h.database.engine.pool.checkedout() == 0
        recorded_texts.extend(texts)
        return original(texts, **kwargs)
    h.embedding.embed = observed
    h.application.decide(op.operation_id, stop=Event())
    first = confirmed_target(h, rid, gid)
    h.application.decide(op.operation_id, stop=Event())
    assert confirmed_target(h, rid, gid) == first
    row = h.knowledge.get_chunks([first])[first]
    assert row.draft.category == '售后手册' and row.vectorize_status == 'done'
    assert recorded_texts == [embedding_text(row.draft)]
    assert len(h.knowledge.all_chunks()) == 1 and vector_count(h) == 1


def test_reject_has_no_target_and_preserves_source(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    with h.database.sessions() as session:
        before = [(m.id, m.content) for m in session.scalars(select(Message).order_by(Message.id))]
    op = queued_decision(h, rid, gid, decision='reject', category=None, reason='单次订单，不适合知识库')
    h.application.decide(op.operation_id, stop=Event())
    assert confirmed_target(h, rid, gid) is None
    assert h.knowledge.all_chunks() == [] and vector_count(h) == 0
    group = h.store.get_group(rid, gid)
    assert {m.reason for m in group.application.prepared.members} == {'human_rejected'}
    with h.database.sessions() as session:
        assert before == [(m.id, m.content) for m in session.scalars(select(Message).order_by(Message.id))]


def test_merge_preserves_existing_text_category_and_vector_count(review_harness):
    h = review_harness
    make_application(h)
    draft = ChunkDraft('旧分类', '旧问法', '擦干后阴凉晾干。', '旧章节', 'manual')
    cid = h.knowledge.import_chunks([draft])[0]
    h.service.vectorize_pending()
    rid, gid = prepared_new_group(h)
    assert h.store.get_group(rid, gid).target_chunk_id == cid
    op = queued_approval(h, rid, gid, category=None)
    result = h.application.decide(op.operation_id, stop=Event())
    assert confirmed_target(h, rid, gid) == cid
    assert h.knowledge.get_chunks([cid])[cid].draft == draft
    assert vector_count(h) == 1 and result.counters.vectorized == 0


def test_stale_or_opposite_decision_conflicts(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    stale = queued_approval(h, rid, gid, key='1'*32, expected_revision=0)
    with pytest.raises(ServiceError) as exc:
        h.application.decide(stale.operation_id, stop=Event())
    assert exc.value.code == 'knowledge_review_stale'
    assert h.store.get_group(rid, gid).decision is None
    good = queued_approval(h, rid, gid)
    h.application.decide(good.operation_id, stop=Event())
    opposite = queued_decision(h, rid, gid, key='2'*32, decision='reject', category=None)
    with pytest.raises(ServiceError):
        h.application.decide(opposite.operation_id, stop=Event())
    assert len(h.knowledge.all_chunks()) == 1


def test_invalid_final_budget_keeps_group_pending(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid, category='合法但超预算的分类')
    def no_budget(text):
        raise ValueError('over budget')
    h.budget.validate = no_budget
    with pytest.raises(ServiceError):
        h.application.decide(op.operation_id, stop=Event())
    assert h.store.get_group(rid, gid).decision is None
    assert h.knowledge.all_chunks() == []
    # Rejecting remains possible, even when there is no embedding budget.
    reject_op = queued_decision(h, rid, gid, key='2'*32, decision='reject', category=None)
    assert h.application.decide(reject_op.operation_id, stop=Event()).phase == 'completed'


def test_sql_commit_before_audit_confirmation_recovers_same_id(review_harness, monkeypatch):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid)
    original = h.store.put_group
    def fail_confirmation(group, **kwargs):
        if group.application and group.application.phase == 'applied':
            raise OSError('confirmation failed')
        return original(group, **kwargs)
    monkeypatch.setattr(h.store, 'put_group', fail_confirmation)
    with pytest.raises(OSError):
        h.application.decide(op.operation_id, stop=Event())
    before = h.knowledge.all_chunks()
    assert len(before) == 1 and before[0].vectorize_status == 'pending'
    assert h.store.get_group(rid, gid).application.phase == 'prepared'
    monkeypatch.setattr(h.store, 'put_group', original)
    h.application.decide(op.operation_id, stop=Event())
    assert confirmed_target(h, rid, gid) == before[0].id
    assert len(h.knowledge.all_chunks()) == 1 and vector_count(h) == 1


def test_prepared_only_and_occupied_id_is_not_committed(review_harness, monkeypatch):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid)
    original = h.repository.apply_group
    def rollback_after_prepared(group, decision, prepare):
        def saved_but_rollback(p):
            prepare(p)
            raise OSError('transaction not committed')
        return original(group, decision, saved_but_rollback)
    monkeypatch.setattr(h.repository, 'apply_group', rollback_after_prepared)
    with pytest.raises(OSError):
        h.application.decide(op.operation_id, stop=Event())
    planned = h.store.get_group(rid, gid).application.prepared.chunk_id
    with h.database.sessions.begin() as session:
        session.add(KnowledgeChunk(id=planned, category='无关', questions='无关问法',
                                   answer='别的答案。', vectorize_status='pending'))
    monkeypatch.setattr(h.repository, 'apply_group', original)
    h.application.decide(op.operation_id, stop=Event())
    target = confirmed_target(h, rid, gid)
    assert target != planned and len(h.knowledge.all_chunks()) == 2
    assert h.knowledge.get_chunks([planned])[planned].vectorize_status == 'pending'


def test_vector_failure_does_not_request_approval_again(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid)
    original = h.embedding.embed
    def fail(*args, **kwargs):
        raise ServiceError('embedding_unavailable', 503, 'safe failure')
    h.embedding.embed = fail
    with pytest.raises(ServiceError):
        h.application.decide(op.operation_id, stop=Event())
    cid = confirmed_target(h, rid, gid)
    assert h.store.get_group(rid, gid).decision.kind == 'approved'
    assert h.knowledge.get_chunks([cid])[cid].vectorize_status == 'pending'
    h.embedding.embed = original
    run = h.application.retry_vectors(rid, stop=Event())
    assert run.phase == 'completed' and confirmed_target(h, rid, gid) == cid
    assert len(h.knowledge.all_chunks()) == 1


def test_seal_corrupt_run_keeps_raw_bytes_and_releases_slot(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    path = h.store.root / 'reviews' / 'runs' / rid / 'run.json'
    path.write_bytes(b'{broken-run')
    op = queued_seal(h)
    marker = h.application.seal(op.operation_id)
    assert marker.run_id == rid and path.read_bytes() == b'{broken-run'
    assert h.store.unfinished() is None
    assert h.knowledge.all_chunks() == []


def test_sealed_queued_decision_never_revives(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid)
    seal = queued_seal(h)
    h.application.seal(seal.operation_id)
    with pytest.raises(ServiceError) as exc:
        h.application.decide(op.operation_id, stop=Event())
    assert exc.value.code == 'knowledge_review_sealed'
    assert h.knowledge.all_chunks() == [] and vector_count(h) == 0


def test_partial_source_in_sealed_run_not_marked_fully_processed(review_harness):
    from retailq.knowledge.review_preparation import processed_sources
    h = review_harness
    rid, gid = prepared_new_group(h)
    # Merely writing an approval intent is not an applied source watermark.
    op = queued_approval(h, rid, gid)
    original = h.repository.apply_group
    def fail(*args):
        raise OSError('before SQL')
    h.repository.apply_group = fail
    with pytest.raises(OSError):
        h.application.decide(op.operation_id, stop=Event())
    h.repository.apply_group = original
    h.application.seal(queued_seal(h).operation_id)
    assert processed_sources(h.store) == frozenset()


@pytest.mark.parametrize('scope,expected_done', [('all',2), ('none',0), ('one',1)])
def test_scoped_vectorize_does_not_touch_unrelated_pending(review_harness, scope, expected_done):
    h = review_harness
    make_application(h)
    ids = [h.knowledge.import_chunks([ChunkDraft('类', '问'+str(i), '答。', None, 'faq')])[0] for i in range(2)]
    selected = None if scope == 'all' else () if scope == 'none' else (ids[1],)
    result = h.service.vectorize_pending(only_ids=selected)
    assert result.done == expected_done and vector_count(h) == expected_done
    assert result.pending_after == (0 if scope != 'none' else 0)
    if scope == 'one':
        assert h.knowledge.get_chunks([ids[0]])[ids[0]].vectorize_status == 'pending'
