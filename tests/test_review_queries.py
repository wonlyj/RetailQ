"""Catch false destination links, hidden audit faults and writes from GET projections."""
from dataclasses import asdict
import json
from threading import Event

import pytest
from sqlalchemy import event, update

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.models import KnowledgeChunk
from tests.support.reviews import (
    RID, review_harness, prepared_new_group, queued_approval, queued_decision, confirmed_target,
    make_queries, make_application,
)

pytestmark = [pytest.mark.mysql, pytest.mark.lite]


def test_prepared_is_not_a_confirmed_destination(review_harness, monkeypatch):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_approval(h, rid, gid)
    original = h.store.put_group
    def stop_confirmation(group, **kwargs):
        if group.application and group.application.phase == 'applied':
            raise OSError('confirmation failed')
        return original(group, **kwargs)
    monkeypatch.setattr(h.store, 'put_group', stop_confirmation)
    with pytest.raises(OSError):
        h.application.decide(op.operation_id, stop=Event())
    view = make_queries(h).group(rid, gid)
    assert view.target_confirmed is False and view.target_chunk_id is None
    assert view.planned_target_id == h.knowledge.all_chunks()[0].id
    assert view.application_phase == 'prepared'


def test_all_member_sources_have_the_same_confirmed_destination(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    h.application.decide(queued_approval(h, rid, gid).operation_id, stop=Event())
    queries = make_queries(h)
    view = queries.group(rid, gid)
    cid = confirmed_target(h, rid, gid)
    assert view.target_chunk_id == cid and view.target_confirmed
    assert len(view.members) == 2
    assert {d.reason for d in view.dispositions} == {'new', 'same_group'}
    assert {m.source_ref for m in view.members} == {s.source_ref for s in queries.run(rid).sources}
    assert queries.chunk(cid).record.draft.category == '售后手册'
    assert queries.run(rid).summary.counters.vectorized == 1
    assert queries.summary().current_run_id is None  # All groups finished.


def test_rejection_has_its_own_disposition_and_no_chunk_link(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    op = queued_decision(h, rid, gid, decision='reject', category=None, reason='不适用')
    h.application.decide(op.operation_id, stop=Event())
    view = make_queries(h).group(rid, gid)
    assert view.summary.disposition == 'rejected' and view.summary.decision == 'rejected'
    assert view.target_chunk_id is None and view.error is None
    assert make_queries(h).groups(rid, disposition='rejected').total == 1


def test_target_drift_is_not_confirmed_by_cached_audit(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    h.application.decide(queued_approval(h, rid, gid).operation_id, stop=Event())
    cid = confirmed_target(h, rid, gid)
    with h.database.sessions.begin() as session:
        session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == cid).values(answer='被改动。'))
    view = make_queries(h).group(rid, gid)
    assert view.target_confirmed is False and view.target_chunk_id is None
    assert view.error.code == 'knowledge_review_conflict'


def test_corrupt_audit_is_visible_and_not_empty_success(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    path = h.store.root / 'reviews' / 'runs' / rid / 'run.json'
    path.write_bytes(b'{broken')
    queries = make_queries(h)
    summary = queries.summary()
    assert summary.current_run_id == rid and summary.counters is None
    assert summary.error.code == 'knowledge_review_corrupt'
    assert queries.runs().total == 1
    assert queries.run(rid).summary.error.code == 'knowledge_review_corrupt'
    assert queries.run(rid).sources is None


def test_read_projection_never_writes_or_invokes_models(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    queries = make_queries(h)
    before = {p: p.read_bytes() for p in h.store.root.rglob('*.json')}
    sql = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.split()[0].upper())
    def forbidden(*args, **kwargs):
        raise AssertionError('GET may not embed')
    h.embedding.embed = forbidden
    event.listen(h.database.engine, 'before_cursor_execute', observe)
    try:
        queries.summary(); queries.runs(); queries.run(rid); queries.group(rid, gid)
        queries.sources(); queries.categories(); queries.groups(rid); queries.operation(rid)
    finally:
        event.remove(h.database.engine, 'before_cursor_execute', observe)
    assert set(sql) <= {'SELECT'}
    assert before == {p: p.read_bytes() for p in h.store.root.rglob('*.json')}


def test_groups_filters_use_real_category_and_question(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    queries = make_queries(h)
    assert queries.groups(rid, disposition='pending').total == 1
    assert queries.groups(rid, disposition='new', query='清洁', category='商品使用与保养').total == 1
    assert queries.groups(rid, category='未选类').total == 0
    assert queries.groups(rid, offset=1).items == ()


def test_group_projection_retries_when_revision_changes_during_read(review_harness, monkeypatch):
    from retailq.knowledge.review_contracts import evolve
    h = review_harness
    rid, gid = prepared_new_group(h)
    original = h.store.get_group
    changed = []
    def concurrent_update(run_id, group_id):
        group = original(run_id, group_id)
        if not changed:
            updated = h.store.put_group(evolve(group, suggestion=evolve(group.suggestion, category='新建议')),
                                         expected_revision=group.revision)
            changed.append(updated.revision)
        return group
    monkeypatch.setattr(h.store, 'get_group', concurrent_update)
    result = make_queries(h).group(rid, gid)
    assert result.summary.revision == changed[0]
    assert result.summary.category == '新建议'


def test_legacy_explicit_ids_are_separate_and_unknown_ids_are_not_guessed(review_harness):
    h = review_harness
    rid, gid = prepared_new_group(h)
    draft = ChunkDraft('旧类', '旧问法', '擦干后阴凉晾干。', None, 'faq')
    cid = h.knowledge.import_chunks([draft])[0]
    group = h.store.get_group(rid, gid)
    path = h.store.root / 'runs' / f'run-{rid}.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'run_id':rid, 'phase':'complete', 'sources':[], 'dedup_groups':[
        {'member_ids':[group.members[0].staging_id], 'target_chunk_id':cid},
        {'member_ids':[group.members[1].staging_id], 'target_chunk_id':None},
    ]}), encoding='utf-8')
    queries = make_queries(h)
    assert queries.run(rid).summary.kind == 'review'
    legacy = queries.legacy_run(rid)
    assert legacy.explicit_targets[0].chunk_id == cid
    assert legacy.explicit_targets[0].disposition == 'legacy_explicit'
    assert legacy.missing_provenance == 1
    with h.database.sessions.begin() as session:
        session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == cid).values(answer='规则已变。'))
    assert queries.legacy_run(rid).missing_provenance == 2
    assert queries.legacy_run(rid).explicit_targets == ()
