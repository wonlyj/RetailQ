"""Catch publication during preparation, lost sources and repeated confirmed stages."""
from threading import Event

import pytest

from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import evolve
from tests.support.reviews import (
    RID, review_harness, seed_closed_source, make_preparer, queued_run_with_sources,
    ReviewGateway, vector_count,
)

pytestmark = [pytest.mark.mysql, pytest.mark.lite]


def two_sources(h):
    return [seed_closed_source(h.database, '怎么清洁？'), seed_closed_source(h.database, '清洁有什么要求？')]


def test_preparation_stops_before_publishing(review_harness):
    h = review_harness
    run = queued_run_with_sources(h, two_sources(h))
    result = make_preparer(h).prepare(run.run_id, stop=Event())
    assert result.phase == 'waiting_review'
    assert h.knowledge.all_chunks() == [] and vector_count(h) == 0
    assert len(result.group_ids) == 1
    group = h.store.get_group(RID, result.group_ids[0])
    assert len(group.members) == 2 and group.decision is None
    assert all(m.message_ids for m in group.members)
    assert group.suggestion.category == '商品使用与保养'


def test_multi_batch_sources_keep_every_staging_member(review_harness):
    h = review_harness
    sources = two_sources(h)
    preparer = make_preparer(h, max_conversations=1)
    run = queued_run_with_sources(h, sources, preparer)
    result = preparer.prepare(run.run_id, stop=Event())
    assert len(result.completed_batches) == 2
    members = h.store.get_group(RID, result.group_ids[0]).members
    assert {m.source_ref for m in members} == {s.source_ref for s in sources}
    assert {mid for m in members for mid in m.message_ids} == {m.id for s in sources for m in s.messages}


def test_prepare_resume_skips_persisted_batches(review_harness):
    h = review_harness
    class OnceFail(ReviewGateway):
        failed = False
        def extract(self, batch):
            if self.extracted_refs and not self.failed:
                self.failed = True
                raise ServiceError('knowledge_qa_invalid', 502, '样例失败')
            return super().extract(batch)
    gateway = OnceFail(h.database)
    preparer = make_preparer(h, gateway, max_conversations=1)
    sources = two_sources(h)
    run = queued_run_with_sources(h, sources, preparer)
    with pytest.raises(ServiceError):
        preparer.prepare(run.run_id, stop=Event())
    assert len(h.store.get_run(RID).completed_batches) == 1
    assert preparer.prepare(RID, stop=Event()).phase == 'waiting_review'
    assert gateway.extracted_refs == [s.source_ref for s in sources]


def test_dedup_plan_survives_partial_group_file_failure(review_harness, monkeypatch):
    h = review_harness
    sources = [seed_closed_source(h.database, '问A', '答A。'), seed_closed_source(h.database, '问B', '答B。')]
    gateway = ReviewGateway()
    preparer = make_preparer(h, gateway)
    queued_run_with_sources(h, sources, preparer)
    original = h.store.put_group
    def fail_second(group, **kwargs):
        if group.members[0].question == '问B':
            raise OSError('disk full')
        return original(group, **kwargs)
    monkeypatch.setattr(h.store, 'put_group', fail_second)
    with pytest.raises(OSError):
        preparer.prepare(RID, stop=Event())
    # A confirmed dedup plan must not depend on rerunning a nondeterministic LLM.
    def forbidden(*args):
        raise AssertionError('confirmed dedup should not run again')
    gateway.deduplicate = forbidden
    monkeypatch.setattr(h.store, 'put_group', original)
    assert preparer.prepare(RID, stop=Event()).phase == 'waiting_review'


def test_rule_change_refuses_resume(review_harness):
    h = review_harness
    run = queued_run_with_sources(h, two_sources(h))
    h.store.put_run(evolve(run, phase='failed'), expected_revision=0)
    preparer = make_preparer(h)
    preparer.input_budget = 31000
    with pytest.raises(ServiceError) as exc:
        preparer.prepare(RID, stop=Event())
    assert exc.value.code == 'knowledge_run_version_mismatch'
    assert h.knowledge.all_chunks() == []


def test_sealed_partial_source_can_be_selected_again(review_harness):
    from retailq.knowledge.review_preparation import processed_sources
    h = review_harness
    sources = two_sources(h)
    queued_run_with_sources(h, sources)
    make_preparer(h).prepare(RID, stop=Event())
    h.store.seal(RID, 'c'*32, '取消审核')
    assert not processed_sources(h.store,h.repository)


def test_no_qa_completes_without_pending_review(review_harness):
    from retailq.knowledge.review_preparation import processed_sources
    h = review_harness
    gateway = ReviewGateway()
    gateway.extract = lambda batch: []
    preparer = make_preparer(h, gateway)
    source = seed_closed_source(h.database)
    queued_run_with_sources(h, [source], preparer)
    result = preparer.prepare(RID, stop=Event())
    assert result.phase == 'completed' and result.group_ids == ()
    assert processed_sources(h.store,h.repository) == frozenset({source.source_ref})
    assert h.knowledge.all_chunks() == [] and vector_count(h) == 0
