from pathlib import Path
import json

import pytest

from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.knowledge.corpus import CorpusStore
from retailq.knowledge.corpus_import import CorpusImporter
from retailq.knowledge.repository import KnowledgeRepository
from tests.support.knowledge import TestBudget
from tests.support.reviews import review_harness


def test_prior_snapshot_only_retires_matching_source_chunk_fingerprint(tmp_path):
    from types import SimpleNamespace
    from retailq.errors import ServiceError
    from retailq.knowledge.contracts import ChunkDraft, ChunkRecord
    from retailq.knowledge.text import content_fingerprint

    old_digest='a'*64;new_digest='b'*64
    draft=ChunkDraft('分类','问题','原始内容',f'policy.md@{old_digest} / 分类 / 块1','policy')
    row=ChunkRecord(12,draft,None,None,'12','done')
    source=SimpleNamespace(key='policy.md',digest=old_digest,chunk_ids=(12,))
    previous=SimpleNamespace(sources=(source,),fingerprints=((12,content_fingerprint(draft)),),retired_ids=())
    corpus=SimpleNamespace(_path=lambda *a: SimpleNamespace(exists=lambda: True),active=lambda:previous)
    repository=SimpleNamespace(all_chunks=lambda:[row])
    importer=CorpusImporter(repository,corpus,TestBudget())
    plan=SimpleNamespace(sources=(SimpleNamespace(key='policy.md',digest=new_digest),))
    assert importer._existing(plan,())[2]==(12,)
    previous.fingerprints=((12,'c'*64),)
    with pytest.raises(ServiceError):importer._existing(plan,())
    previous.fingerprints=((12,content_fingerprint(draft)),)
    source.chunk_ids=(13,)
    with pytest.raises(ServiceError):importer._existing(plan,())


@pytest.mark.mysql
def test_committed_import_resumes_same_ids(test_database, tmp_path):
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n满99元免运费，未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    identity = RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', identity))
    importer = CorpusImporter(KnowledgeRepository(test_database), corpus, TestBudget())
    committed = importer.prepare(manifest, operation_id='a' * 32)
    resumed = importer.resume('a' * 32)
    assert resumed.active_ids == committed.active_ids
    assert len(resumed.active_ids) == 1


@pytest.mark.mysql
def test_resume_after_sql_commit_before_checkpoint_does_not_duplicate(test_database, tmp_path, monkeypatch):
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n满99元免运费，未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    coordinator = Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root))
    repository = KnowledgeRepository(test_database)
    original = repository.import_chunks
    def crash_after_commit(drafts):
        original(drafts)
        raise RuntimeError('simulated interruption')
    monkeypatch.setattr(repository, 'import_chunks', crash_after_commit)
    importer = CorpusImporter(repository, CorpusStore(root, coordinator), TestBudget())
    with pytest.raises(RuntimeError):
        importer.prepare(manifest, operation_id='b' * 32)
    existing = repository.all_chunks()
    monkeypatch.setattr(repository, 'import_chunks', original)
    resumed = importer.resume('b' * 32)
    assert resumed.active_ids == tuple(row.id for row in existing)
    assert len(repository.all_chunks()) == len(existing)


@pytest.mark.mysql
def test_custom_shipping_faq_blocks_import_without_overwrite(test_database, tmp_path):
    from sqlalchemy import text
    from retailq.errors import ServiceError

    with test_database.engine.begin() as connection:
        connection.execute(text("UPDATE faq SET answer='自定义运费8元' WHERE id=2"))
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    importer = CorpusImporter(KnowledgeRepository(test_database), corpus, TestBudget())
    with pytest.raises(ServiceError):
        importer.prepare(manifest, operation_id='c' * 32)
    with test_database.engine.connect() as connection:
        assert connection.scalar(text('SELECT answer FROM faq WHERE id=2')) == '自定义运费8元'


@pytest.mark.mysql
def test_approved_ids_remain_frozen_across_importer_restart(test_database, tmp_path, monkeypatch):
    from retailq.knowledge.contracts import ChunkDraft

    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    repository = KnowledgeRepository(test_database)
    approved_id = repository.import_chunks([ChunkDraft('审核知识', '示例问题', '示例答案', None, 'faq')])[0]
    original = repository.import_chunks
    def crash(drafts):
        original(drafts)
        raise RuntimeError('simulated interruption')
    monkeypatch.setattr(repository, 'import_chunks', crash)
    with pytest.raises(RuntimeError):
        CorpusImporter(repository, corpus, TestBudget(), approved_ids=(approved_id,), approval_resolver=lambda: (approved_id,)).prepare(manifest, operation_id='d' * 32)
    monkeypatch.setattr(repository, 'import_chunks', original)
    resumed = CorpusImporter(repository, corpus, TestBudget(), approval_resolver=lambda: (approved_id,)).resume('d' * 32)
    assert approved_id in resumed.approved_ids
    assert approved_id in resumed.active_ids


@pytest.mark.mysql
def test_damaged_archive_is_rejected_before_shipping_update(test_database, tmp_path, monkeypatch):
    from sqlalchemy import text
    from retailq.errors import ServiceError

    with test_database.engine.begin() as connection:
        connection.execute(text("UPDATE faq SET answer=REPLACE(answer, '收取10元', '收取8元') WHERE id=2"))

    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    repository = KnowledgeRepository(test_database)
    original = repository.migrate_known_shipping_faq
    def crash():
        raise RuntimeError('before SQL update')
    monkeypatch.setattr(repository, 'migrate_known_shipping_faq', crash)
    importer = CorpusImporter(repository, corpus, TestBudget())
    with pytest.raises(RuntimeError):
        importer.prepare(manifest, operation_id='e' * 32)
    plan = corpus.get_plan('e' * 32)
    corpus._path(*plan.sources[0].archive.split('/')).write_text('damaged', encoding='utf-8')
    monkeypatch.setattr(repository, 'migrate_known_shipping_faq', original)
    with pytest.raises(ServiceError):
        importer.resume('e' * 32)
    with test_database.engine.connect() as connection:
        assert '收取8元' in connection.scalar(text('SELECT answer FROM faq WHERE id=2'))


@pytest.mark.mysql
def test_reference_documents_import_with_exact_section_ranges(test_database, tmp_path):
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    repository = KnowledgeRepository(test_database)
    plan = CorpusImporter(repository, corpus, TestBudget()).prepare(Path('examples/knowledge/manifest.json'), operation_id='f' * 32)
    assert len(plan.sources) == 5
    assert len(plan.active_ids) == 53
    records = repository.get_chunks(list(plan.active_ids))
    for source in plan.sources:
        archived = corpus._path(*source.archive.split('/')).read_text(encoding='utf-8').splitlines()
        assert len(source.line_ranges) == len(source.chunk_ids)
        for chunk_id, start, end in source.line_ranges:
            assert 1 <= start <= end <= len(archived)
            section = '\n'.join(archived[start - 1:end])
            assert all(line.strip() in section for line in records[chunk_id].draft.answer.splitlines() if line.strip())


@pytest.mark.mysql
def test_known_legacy_seed_preserves_other_faq_and_retires_shipping(test_database, tmp_path):
    repository = KnowledgeRepository(test_database)
    draft_rows = repository.faq_drafts()
    ids = [repository.import_chunks([draft])[0] for draft in draft_rows]
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    plan = CorpusImporter(repository, corpus, TestBudget()).prepare(manifest, operation_id='1' * 32)
    assert set(plan.approved_ids) == {ids[0], ids[2]}
    assert ids[1] in plan.retired_ids
    assert ids[1] not in plan.active_ids


@pytest.mark.mysql
def test_unverified_approved_ids_are_rejected(test_database, tmp_path):
    from retailq.errors import ServiceError
    from retailq.knowledge.contracts import ChunkDraft

    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    repository = KnowledgeRepository(test_database)
    unverified = repository.import_chunks([ChunkDraft('审核知识', '问题', '答案', None, 'faq')])[0]
    with pytest.raises(ServiceError):
        CorpusImporter(repository, corpus, TestBudget(), approved_ids=(unverified,)).prepare(manifest, operation_id='2' * 32)


@pytest.mark.mysql
@pytest.mark.standalone
def test_review_approval_resolver_requires_committed_application(review_harness):
    from threading import Event
    from sqlalchemy import text
    from retailq.errors import ServiceError
    from retailq.knowledge.corpus_import import ReviewApprovalResolver
    from tests.support.reviews import prepared_new_group, queued_approval, make_application, confirmed_target

    harness = review_harness
    run_id, group_id = prepared_new_group(harness)
    operation = queued_approval(harness, run_id, group_id)
    make_application(harness).decide(operation.operation_id, stop=Event())
    target = confirmed_target(harness, run_id, group_id)
    resolver = ReviewApprovalResolver(harness.store, harness.repository)
    assert target in resolver()
    with harness.database.engine.begin() as connection:
        connection.execute(text("UPDATE qa_extraction_staging SET status='extracted'"))
    with pytest.raises(ServiceError):
        resolver()


@pytest.mark.mysql
def test_resume_rejects_tampered_source_location(test_database, tmp_path):
    from sqlalchemy import text
    from retailq.errors import ServiceError

    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    importer = CorpusImporter(KnowledgeRepository(test_database), corpus, TestBudget())
    plan = importer.prepare(manifest, operation_id='3' * 32)
    with test_database.engine.begin() as connection:
        connection.execute(text("UPDATE knowledge_chunks SET section_path='错误章节' WHERE id=:id"), {'id': plan.active_ids[0]})
    with pytest.raises(ServiceError):
        importer.resume(plan.build_id)


@pytest.mark.mysql
def test_resume_rejects_damaged_chunk_chain(test_database, tmp_path):
    from sqlalchemy import text
    from retailq.errors import ServiceError
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'policy.md').write_text('# 演示政策\n## 运费\n未满收10元。\n## 其他\n以结算页为准。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root)))
    importer = CorpusImporter(KnowledgeRepository(test_database), corpus, TestBudget())
    plan = importer.prepare(manifest, operation_id='4' * 32)
    assert len(plan.active_ids) == 2
    with test_database.engine.begin() as connection:
        connection.execute(text('UPDATE knowledge_chunks SET prev_chunk_id=NULL WHERE id=:id'), {'id': plan.active_ids[1]})
    with pytest.raises(ServiceError):
        importer.resume(plan.build_id)
