"""Catch lost audit writes, stale decisions, mutable snapshots and unsafe identities."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import os

import pytest
from pydantic import ValidationError

from retailq.errors import ServiceError
from tests.support.reviews import RID, make_store, make_run, make_operation


def test_roundtrip_cas_and_seal_priority(tmp_path):
    store = make_store(tmp_path)
    first = store.put_run(make_run(), expected_revision=None)
    assert store.get_run(RID).sources == first.sources
    with pytest.raises(ServiceError):
        store.put_run(first, expected_revision=None)
    changed = store.put_run(first, expected_revision=0)
    assert changed.revision == 1
    with pytest.raises(ServiceError):
        store.put_run(first, expected_revision=0)
    marker = store.seal(RID, 'b' * 32, '人工终止')
    assert marker.actor == '本机操作'
    assert store.get_run(RID).phase == 'sealed'
    assert store.unfinished() is None
    assert store.seal(RID, 'b' * 32, '人工终止') == marker


@pytest.mark.parametrize('boundary', ['fsync', 'replace'])
def test_fsync_or_replace_failure_preserves_old_record(tmp_path, monkeypatch, boundary):
    store = make_store(tmp_path)
    first = store.put_run(make_run(), expected_revision=None)
    path = tmp_path / 'reviews' / 'runs' / RID / 'run.json'
    original = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('disk failed')
    monkeypatch.setattr(os, boundary, fail)
    with pytest.raises(OSError):
        store.put_run(first, expected_revision=0)
    assert path.read_bytes() == original
    assert store.get_run(RID).revision == 0


def test_corrupt_record_blocks_new_run_and_can_be_sealed(tmp_path):
    from retailq.knowledge.review_contracts import AuditFault
    store = make_store(tmp_path)
    store.put_run(make_run(), expected_revision=None)
    path = tmp_path / 'reviews' / 'runs' / RID / 'run.json'
    path.write_bytes(b'{broken')
    assert isinstance(store.unfinished(), AuditFault)
    with pytest.raises(ServiceError):
        store.put_run(make_run('b' * 32), expected_revision=None)
    store.seal(RID, 'c' * 32, '人工终止')
    assert store.unfinished() is None
    assert path.read_bytes() == b'{broken'
    store.put_run(make_run('b' * 32), expected_revision=None)


def test_operation_identity_mismatch_is_a_visible_fault_not_a_trusted_binding(tmp_path):
    from retailq.knowledge.review_contracts import AuditFault
    store=make_store(tmp_path)
    store.put_operation(make_operation(),expected_revision=None)
    original=tmp_path/'reviews/operations'/f'{RID}.json'
    wrong=original.with_name('f'*32+'.json')
    wrong.write_bytes(original.read_bytes())
    with pytest.raises(ServiceError):
        store.get_operation('f'*32)
    faults=[r for r in store.list_runs() if isinstance(r,AuditFault)]
    assert any(r.run_id=='f'*32 for r in faults)


def test_idempotency_key_different_command_conflicts(tmp_path):
    store = make_store(tmp_path)
    op = make_operation()
    store.put_operation(op, expected_revision=None)
    assert store.put_operation(op, expected_revision=None) == op
    with pytest.raises(ServiceError) as exc:
        store.put_operation(make_operation(ids=(2,)), expected_revision=None)
    assert exc.value.code == 'knowledge_request_conflict'


def test_queued_command_without_run_still_occupies_slot_without_read_writes(tmp_path):
    store = make_store(tmp_path)
    store.put_operation(make_operation(), expected_revision=None)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    run = store.unfinished()
    assert run.run_id == RID and run.phase == 'interrupted'
    assert run.sources[0].conversation_id == 1
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    with pytest.raises(ServiceError):
        store.put_run(make_run('b' * 32), expected_revision=None)


def test_nested_records_cannot_be_mutated(tmp_path):
    store = make_store(tmp_path)
    record = store.put_run(make_run(), expected_revision=None)
    with pytest.raises(ValidationError):
        record.phase = 'completed'
    with pytest.raises(FrozenInstanceError):
        record.sources[0].messages[0].content = 'changed'
    assert isinstance(record.sources, tuple)
    assert isinstance(record.sources[0].messages, tuple)
    assert store.get_run(RID) == record


@pytest.mark.parametrize('value', ['../x', 'A'*32, 'f'*33, '', 'a/b'])
def test_path_ids_are_rejected(tmp_path, value):
    with pytest.raises(ServiceError):
        make_store(tmp_path).get_run(value)


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / 'knowledge'
    outside = tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    # Windows directory junction requires no developer-mode symlink privilege.
    if os.name == 'nt':
        import subprocess
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'reviews'), str(outside)],
                       check=True, capture_output=True)
    else:
        (root / 'reviews').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ServiceError):
        make_store(root).put_run(make_run(), expected_revision=None)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('value', [True, 1, 1.0, '0', '01', '-1', '１２', '1.0', str(2**63)])
def test_invalid_db_ids_are_rejected(value):
    from retailq.knowledge.review_contracts import parse_db_id
    with pytest.raises(ValueError):
        parse_db_id(value)


def test_bigint_and_canonical_client_intent():
    from retailq.knowledge.review_contracts import parse_db_id, canonical_request_hash, PrepareCommand
    assert parse_db_id('9007199254740993') == 9007199254740993
    a = PrepareCommand(request_key=RID, conversation_ids=(2, 1))
    b = PrepareCommand(request_key=RID, conversation_ids=(1, 2))
    assert canonical_request_hash(a) == canonical_request_hash(b)
    assert canonical_request_hash(a) != canonical_request_hash(PrepareCommand(request_key=RID))
    with pytest.raises(ValidationError):
        PrepareCommand(request_key=RID, conversation_ids=(1, 1))
    with pytest.raises(ValidationError):
        PrepareCommand(request_key=RID, conversation_ids=(True,))


@pytest.mark.parametrize('value', ['', '   ', 'x'*256, 'a\x00b', 'a\u200bb', '\nx'])
def test_invalid_categories_are_rejected(value):
    from retailq.knowledge.review_contracts import normalize_category
    with pytest.raises(ValueError):
        normalize_category(value)


def test_category_nfc_and_group_contract():
    from retailq.knowledge.review_contracts import normalize_category, ReviewGroup, SourceQA
    assert normalize_category('  cafe\u0301 ') == 'café'
    member = SourceQA(staging_id=1, batch_no='c'*64, source_ref='conversation:1:message:2',
                      question='怎么清洁？', answer='阴凉晾干。', message_ids=(1, 2))
    group = ReviewGroup(run_id=RID, group_id='b'*32, members=(member,),
                        representative_id=1, questions=('怎么清洁？',), answer='阴凉晾干。')
    assert ReviewGroup.model_validate_json(group.model_dump_json()) == group
    with pytest.raises(ValidationError):
        ReviewGroup.model_validate({**group.model_dump(), 'target_chunk_id': 2})


def test_operation_update_cannot_replace_frozen_sources(tmp_path):
    from retailq.knowledge.review_contracts import evolve
    store = make_store(tmp_path)
    op = store.put_operation(make_operation(), expected_revision=None)
    with pytest.raises(ServiceError):
        store.put_operation(evolve(op, source_snapshot=()), expected_revision=0)
    assert store.get_operation(RID).source_snapshot == op.source_snapshot
