from pathlib import Path

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.mysql


def draft(answer='演示规则：满99元免运费。', position=1):
    from retailq.knowledge.contracts import ChunkDraft
    return ChunkDraft('配送', '运费标准', answer, f'shipping@v1/运费/块{position}', 'policy', True)


def repo(database):
    from retailq.knowledge.repository import KnowledgeRepository
    return KnowledgeRepository(database)


def test_six_tables_match_supplied_ddl(test_database):
    from retailq.models import Base
    assert set(inspect(test_database.engine).get_table_names()) == set(Base.metadata.tables)
    cols = {c['name']: c for c in inspect(test_database.engine).get_columns('knowledge_chunks')}
    assert cols['id']['type'].unsigned
    assert cols['vectorize_status']['type'].enums == ['pending', 'done']
    assert len(inspect(test_database.engine).get_foreign_keys('knowledge_chunks')) == 2


def test_import_reuses_identical_sequence_and_ids(test_database):
    r = repo(test_database)
    drafts = [draft(position=1), draft('演示规则：未满99元收8元。', 2)]
    ids = r.import_chunks(drafts)
    assert r.import_chunks(drafts) == ids
    assert len(r.all_chunks()) == 2
    rows = r.get_chunks(ids)
    assert rows[ids[0]].next_chunk_id == ids[1]
    assert rows[ids[1]].prev_chunk_id == ids[0]
    assert all(row.vectorize_status == 'pending' for row in rows.values())


def test_repeated_paragraphs_keep_distinct_positions(test_database):
    r = repo(test_database)
    ids = r.import_chunks([draft(position=1), draft(position=2)])
    assert len(set(ids)) == 2
    assert r.import_chunks([draft(position=1), draft(position=2)]) == ids


def test_same_text_different_case_or_accent_is_not_equal(test_database):
    r = repo(test_database)
    ids = [r.import_chunks([draft(value)])[0] for value in ['Cafe。', 'CAFE。', 'Café。']]
    assert len(set(ids)) == 3


def test_pointer_failure_rolls_back_whole_file(test_database):
    r = repo(test_database)
    def fail(_):
        raise ValueError('forced commit failure')
    event.listen(Session, 'before_commit', fail)
    try:
        with pytest.raises(ValueError):
            r.import_chunks([draft(position=1), draft(position=2)])
    finally:
        event.remove(Session, 'before_commit', fail)
    assert r.all_chunks() == []
    assert test_database.engine.pool.checkedout() == 0


def test_mark_done_checks_fingerprint(test_database):
    from retailq.knowledge.text import content_fingerprint
    r = repo(test_database)
    d = draft()
    chunk_id = r.import_chunks([d])[0]
    assert not r.mark_done(chunk_id, str(chunk_id), 'wrong')
    assert r.pending(10)[0].id == chunk_id
    assert r.mark_done(chunk_id, str(chunk_id), content_fingerprint(d))
    assert r.pending(10) == []
    assert r.get_chunks([chunk_id])[chunk_id].vector_id == str(chunk_id)
    assert r.reset_pending([chunk_id]) == 1
    assert r.pending(10)[0].vector_id is None


def test_migration_is_idempotent_and_resumes_partial_schema(test_database):
    from scripts.migrate_knowledge import migrate
    with test_database.engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE qa_extraction_staging')
    migrate(test_database, Path('db/init/03-knowledge.sql'))
    migrate(test_database, Path('db/init/03-knowledge.sql'))
    assert inspect(test_database.engine).has_table('qa_extraction_staging')
    with test_database.sessions() as session:
        assert session.scalar(text('SELECT COUNT(*) FROM faq')) == 3


def test_migration_rejects_existing_drift_without_rewriting(test_database):
    from scripts.migrate_knowledge import migrate
    from retailq.errors import ServiceError
    with test_database.engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE qa_extraction_staging MODIFY batch_no VARCHAR(63) NOT NULL')
    try:
        with pytest.raises(ServiceError):
            migrate(test_database, Path('db/init/03-knowledge.sql'))
    finally:
        with test_database.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE qa_extraction_staging MODIFY batch_no VARCHAR(64) NOT NULL')


def test_migration_rejects_changed_status_default(test_database):
    from scripts.migrate_knowledge import migrate
    from retailq.errors import ServiceError
    with test_database.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE knowledge_chunks ALTER vectorize_status SET DEFAULT 'done'")
    try:
        with pytest.raises(ServiceError):
            migrate(test_database,Path('db/init/03-knowledge.sql'))
    finally:
        with test_database.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE knowledge_chunks ALTER vectorize_status SET DEFAULT 'pending'")
