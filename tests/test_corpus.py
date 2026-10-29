from dataclasses import replace

import pytest

from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.errors import ServiceError
from retailq.knowledge.corpus import CorpusPlan, CorpusSnapshot, CorpusStore, is_active
from retailq.knowledge.contracts import ChunkDraft, ChunkRecord
from retailq.knowledge.text import content_fingerprint


def store(tmp_path):
    identity = RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path / 'knowledge')
    return CorpusStore(tmp_path / 'knowledge', Coordinator(tmp_path / 'locks', identity))


def test_unverified_build_does_not_change_active_corpus(tmp_path):
    corpus = store(tmp_path)
    plan = CorpusPlan(build_id='a' * 32, previous_generation=0, collection='knowledge_a',
                      identity_hash='b' * 64, sources=(), active_ids=(), retired_ids=(), fingerprints=(), phase='prepared')
    corpus.stage(plan)
    with pytest.raises(ServiceError):
        corpus.active()
    with pytest.raises(ServiceError):
        corpus.publish(plan.build_id, expected_generation=0)


def test_published_snapshot_checks_ids_and_fingerprint(tmp_path):
    corpus = store(tmp_path)
    draft = ChunkDraft('演示配送', '运费？', '未满99元收10元', 'shipping.md@' + 'd' * 64 + ' / 配送 / 块1', 'policy')
    row = ChunkRecord(7, draft, None, None, '7', 'done')
    plan = CorpusPlan(build_id='a' * 32, previous_generation=0, collection='knowledge_a',
                      identity_hash='b' * 64, sources=(), active_ids=(7,), retired_ids=(8,),
                      fingerprints=((7, content_fingerprint(draft)),), phase='prepared', approved_ids=(7,))
    corpus.stage(plan)
    for previous, phase in [('prepared', 'sql_committed'), ('sql_committed', 'indexing'), ('indexing', 'verified')]:
        plan = replace(plan, phase=phase)
        corpus.update(plan, expected_phase=previous)
    snapshot = corpus.publish(plan.build_id, expected_generation=0)
    assert snapshot.generation == 1
    assert is_active(row, snapshot)
    assert not is_active(replace(row, id=8), snapshot)
    assert not is_active(replace(row, draft=replace(draft, answer='未满99元收8元')), snapshot)
    with pytest.raises(ServiceError):
        corpus.publish(plan.build_id, expected_generation=0)


def test_build_cannot_skip_phases_or_replace_identity(tmp_path):
    corpus = store(tmp_path)
    plan = CorpusPlan(build_id='a' * 32, previous_generation=0, collection='knowledge_a',
                      identity_hash='b' * 64, sources=(), active_ids=(), retired_ids=(), fingerprints=(), phase='prepared')
    corpus.stage(plan)
    with pytest.raises(ServiceError):
        corpus.update(replace(plan, phase='verified'), expected_phase='prepared')
    with pytest.raises(ServiceError):
        corpus.update(replace(plan, phase='sql_committed', collection='knowledge_other'), expected_phase='prepared')


def test_publish_recovers_if_active_written_before_build_record(tmp_path, monkeypatch):
    import retailq.knowledge.corpus as module

    corpus = store(tmp_path)
    plan = CorpusPlan(build_id='a' * 32, previous_generation=0, collection='knowledge_a',
                      identity_hash='b' * 64, sources=(), active_ids=(), retired_ids=(), fingerprints=(), phase='prepared')
    corpus.stage(plan)
    for previous, phase in [('prepared', 'sql_committed'), ('sql_committed', 'indexing'), ('indexing', 'verified')]:
        plan = replace(plan, phase=phase)
        corpus.update(plan, expected_phase=previous)
    original = module._write
    def fail_build(path, value):
        if path.name == plan.build_id + '.json':
            raise OSError('simulated crash')
        original(path, value)
    monkeypatch.setattr(module, '_write', fail_build)
    with pytest.raises(OSError):
        corpus.publish(plan.build_id, expected_generation=0)
    monkeypatch.setattr(module, '_write', original)
    assert corpus.publish(plan.build_id, expected_generation=0).generation == 1
    assert corpus.active().generation == 1
