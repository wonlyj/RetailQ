"""Catch accidental publication, incomplete SQL commits and guessed provenance."""
import pytest
from sqlalchemy import select, update, delete

from retailq.errors import ServiceError
from retailq.models import Conversation, Message
from retailq.knowledge.models import KnowledgeChunk, QAStaging
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.review_contracts import evolve, ApplicationRecord
from retailq.knowledge.text import content_fingerprint
from tests.support.reviews import (
    review_harness, seed_new_group, approve, reject, group_batch_ids, seed_closed_source, vector_count,
)

pytestmark = [pytest.mark.mysql, pytest.mark.lite]


def with_prepared(group, decision, prepared):
    return evolve(group, decision=decision, application=ApplicationRecord(phase='prepared', prepared=prepared))


def test_prepared_failure_rolls_back_group_and_chunk(review_harness):
    h = review_harness
    group = seed_new_group(h)
    assert h.knowledge.all_chunks() == [] and vector_count(h) == 0
    observed = []
    def fail_disk_write(prepared):
        observed.append(prepared.chunk_id)
        assert prepared.chunk_id > 0
        # Another session cannot see the flushed but uncommitted chunk.
        assert h.knowledge.get_chunks([prepared.chunk_id]) == {}
        raise OSError('disk full')
    with pytest.raises(OSError):
        h.repository.apply_group(group, approve(), fail_disk_write)
    assert observed
    assert h.knowledge.all_chunks() == []
    assert {r.status for r in h.knowledge.staging(group_batch_ids(group))} == {'extracted'}
    assert h.database.engine.pool.checkedout() == 0


def test_committed_application_is_confirmed_without_guessing_text(review_harness):
    h = review_harness
    group = seed_new_group(h)
    decision = approve('售后手册')
    prepared = h.repository.apply_group(group, decision, lambda p: None)
    recovered = with_prepared(group, decision, prepared)
    assert h.repository.inspect_application(recovered) == 'committed'
    row = h.knowledge.get_chunks([prepared.chunk_id])[prepared.chunk_id]
    assert row.draft.category == '售后手册' and row.vectorize_status == 'pending'
    assert [(m.status, m.reason, m.chunk_id) for m in prepared.members] == [
        ('kept', 'new', row.id), ('discarded', 'same_group', row.id)]
    assert len(h.knowledge.all_chunks()) == 1 and vector_count(h) == 0


def test_pending_member_and_occupied_prepared_id_not_treated_as_committed(review_harness):
    h = review_harness
    group = seed_new_group(h)
    decision = approve()
    saved = []
    def rollback(p):
        saved.append(p)
        raise OSError('abort')
    with pytest.raises(OSError):
        h.repository.apply_group(group, decision, rollback)
    with h.database.sessions.begin() as session:
        session.add(KnowledgeChunk(id=saved[0].chunk_id, category='其他', questions='无关问法',
                                   answer='无关答案。', vectorize_status='pending'))
    assert h.repository.inspect_application(with_prepared(group, decision, saved[0])) == 'not_committed'


@pytest.mark.parametrize('drift', ['member_content', 'member_status', 'missing_target', 'target_content'])
def test_member_drift_or_missing_target_conflicts(review_harness, drift):
    h = review_harness
    group = seed_new_group(h)
    decision = approve()
    prepared = h.repository.apply_group(group, decision, lambda p: None)
    with h.database.sessions.begin() as session:
        if drift == 'member_content':
            session.execute(update(QAStaging).where(QAStaging.id == group.members[0].staging_id).values(answer='被改过。'))
        elif drift == 'member_status':
            session.execute(update(QAStaging).where(QAStaging.id == group.members[0].staging_id).values(status='extracted'))
        elif drift == 'missing_target':
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id == prepared.chunk_id))
        else:
            session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == prepared.chunk_id).values(category='已改类'))
    assert h.repository.inspect_application(with_prepared(group, decision, prepared)) == 'conflict'


def test_rejection_records_explicit_no_destination(review_harness):
    h = review_harness
    group = seed_new_group(h)
    decision = reject()
    p = h.repository.apply_group(group, decision, lambda p: None)
    assert p.chunk_id is None and p.fingerprint is None
    assert {m.reason for m in p.members} == {'human_rejected'}
    assert h.repository.inspect_application(with_prepared(group, decision, p)) == 'committed'
    assert h.knowledge.all_chunks() == []


def test_merge_preserves_target_and_all_sources_point_to_same_id(review_harness):
    h = review_harness
    draft = ChunkDraft('原分类', '原问法', '原答案。', '原章节', 'manual')
    cid = h.knowledge.import_chunks([draft])[0]
    group = evolve(seed_new_group(h), representative_id=None, target_chunk_id=cid,
                    target_fingerprint=content_fingerprint(draft))
    p = h.repository.apply_group(group, approve(None), lambda p: None)
    assert {m.chunk_id for m in p.members} == {cid}
    assert {m.reason for m in p.members} == {'merge_existing'}
    assert h.knowledge.get_chunks([cid])[cid].draft == draft
    assert len(h.knowledge.all_chunks()) == 1


@pytest.mark.parametrize('status,last_role,answer', [
    ('进行中', 'assistant', '答案。'), ('已转人工', 'assistant', '答案。'),
    ('已结束', 'tool', '答案。'), ('已结束', 'assistant', '  '),
])
def test_ineligible_sources_and_mixed_selection_are_rejected(review_harness, status, last_role, answer):
    h = review_harness
    good = seed_closed_source(h.database)
    bad = seed_closed_source(h.database, answer=answer)
    with h.database.sessions.begin() as session:
        session.execute(update(Conversation).where(Conversation.id == bad.conversation_id).values(status=status))
        session.execute(update(Message).where(Message.id == bad.max_message_id).values(role=last_role))
    rows = h.repository.list_sources(eligibility='ineligible').items
    assert any(r.id == bad.conversation_id and r.reason for r in rows)
    with pytest.raises(ServiceError):
        h.repository.freeze_sources((good.conversation_id, bad.conversation_id))
    assert h.repository.freeze_sources((good.conversation_id,)) == (good,)
    with pytest.raises(ServiceError):
        h.repository.freeze_sources((good.conversation_id, 2**63 - 1))


def test_categories_preserve_exact_case_accent_and_pagination(review_harness):
    h = review_harness
    for i, category in enumerate(('Cafe', 'CAFE', 'Café', 'Cafe')):
        h.knowledge.import_chunks([ChunkDraft(category, f'问{i}', '答案。', None, 'faq')])
    page = h.repository.categories(limit=2)
    assert page.total == 3 and len(page.items) == 2
    assert set(page.items + h.repository.categories(offset=2, limit=2).items) == {'Cafe', 'CAFE', 'Café'}
    assert h.repository.categories(query='Café').items == ('Café',)


@pytest.mark.parametrize('ids', [(), (1, 1), (True,), tuple(range(1, 102))])
def test_freeze_rejects_invalid_selection_before_sql(review_harness, ids):
    with pytest.raises((ValueError, ServiceError)):
        review_harness.repository.freeze_sources(ids)
