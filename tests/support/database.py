from pathlib import Path

import pytest

from retailq.config import Settings


@pytest.fixture
def test_database():
    from retailq.db import Database
    from sqlalchemy import text
    settings = Settings(mysql_port=3307, mysql_database="retailq_test")
    assert settings.mysql_port == 3307 and settings.mysql_database == 'retailq_test'
    database = Database(settings)
    try:
        database.ping()
    except Exception:
        database.close()
        pytest.fail('Start isolated MySQL: docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql', pytrace=False)
    with database.sessions.begin() as session:
        for name in ('tickets', 'messages', 'faq', 'conversations'):
            session.execute(text(f'DELETE FROM {name}'))
        for statement in Path('db/init/02-seed.sql').read_text(encoding='utf-8').split(';'):
            if statement.strip():
                session.connection().exec_driver_sql(statement)
    try:
        yield database
    finally:
        database.close()
