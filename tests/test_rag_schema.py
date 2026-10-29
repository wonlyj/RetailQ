from hashlib import sha256
from pathlib import Path
import subprocess

import pytest


def test_chapter_four_ddl_matches_source():
    copied = Path('db/init/04-rag.sql').read_bytes()
    assert sha256(copied).hexdigest() == '8840159feb482e325db6b052e99eb646e1758d7bfe3489d7ab184e535b0df118'


def test_chapter_four_ddl_is_not_line_ending_converted():
    result = subprocess.run(
        ['git', '-c', f'safe.directory={Path.cwd().resolve().as_posix()}', 'check-attr', 'text', '--', 'db/init/04-rag.sql'],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip().endswith('text: unset')


def test_modified_ddl_is_rejected_before_database_access(tmp_path):
    from scripts.migrate_rag import migrate_rag

    class Database:
        @property
        def engine(self):
            raise AssertionError('database must not be accessed')

    path = tmp_path / 'modified.sql'
    path.write_text('CREATE TABLE wrong (id INT);', encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        migrate_rag(Database(), path)


@pytest.mark.mysql
def test_rag_migration_is_idempotent_and_matches_schema(test_database):
    from sqlalchemy import inspect
    from scripts.migrate_rag import migrate_rag

    ddl = Path('db/init/04-rag.sql')
    migrate_rag(test_database, ddl)
    migrate_rag(test_database, ddl)
    with test_database.engine.connect() as connection:
        inspector = inspect(connection)
        assert inspector.has_table('low_confidence_questions')
        assert inspector.has_table('faith_cases')
        assert any(i['name'] == 'uk_eval_id' and i['unique']
                   for i in inspector.get_indexes('faith_cases'))


@pytest.mark.mysql
def test_isolated_reset_clears_rag_rows_before_conversations(test_database):
    from sqlalchemy import text
    from retailq.config import Settings
    from tests.support.database import reset_isolated_database

    with test_database.engine.begin() as connection:
        connection.execute(text("INSERT INTO low_confidence_questions (conversation_id, raw_question, source) VALUES (1, '测试', 'self_check')"))
    fresh = reset_isolated_database(Settings(mysql_port=3307, mysql_database='retailq_test'))
    try:
        with fresh.engine.connect() as connection:
            assert connection.scalar(text('SELECT COUNT(*) FROM low_confidence_questions')) == 0
    finally:
        fresh.close()


@pytest.mark.mysql
def test_migration_rejects_nonunique_eval_index(test_database):
    from sqlalchemy import text
    from retailq.errors import ServiceError
    from scripts.migrate_rag import migrate_rag

    with test_database.engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE faith_cases DROP INDEX uk_eval_id, ADD INDEX uk_eval_id (eval_id)')
    try:
        with pytest.raises(ServiceError):
            migrate_rag(test_database, Path('db/init/04-rag.sql'))
    finally:
        with test_database.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE faith_cases DROP INDEX uk_eval_id, ADD UNIQUE INDEX uk_eval_id (eval_id)')


@pytest.mark.mysql
def test_migration_checks_existing_tables_before_creating_missing(test_database, monkeypatch):
    from sqlalchemy import event
    from retailq.errors import ServiceError
    import scripts.migrate_rag as migration

    class Inspector:
        def has_table(self, name):
            return name == 'faith_cases'

    statements = []
    def observed(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def reject(connection, table):
        raise ServiceError('knowledge_schema_mismatch', 409, '结构不匹配')

    monkeypatch.setattr(migration, 'inspect', lambda connection: Inspector())
    monkeypatch.setattr(migration, 'verify_rag_table', reject)
    event.listen(test_database.engine, 'before_cursor_execute', observed)
    try:
        with pytest.raises(ServiceError):
            migration.migrate_rag(test_database, Path('db/init/04-rag.sql'))
    finally:
        event.remove(test_database.engine, 'before_cursor_execute', observed)
    assert not any('CREATE TABLE' in statement.upper() for statement in statements)
