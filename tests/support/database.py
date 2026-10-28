from pathlib import Path

import pytest

from retailq.config import Settings


def chat_settings(url, **kwargs):
    from tests.test_gateway import settings_for
    local = Settings()
    return settings_for(url, mysql_host='127.0.0.1',mysql_port=3307,mysql_database='retailq_test',
                        mysql_user=local.mysql_user,mysql_password=local.mysql_password,**kwargs)


def reset_isolated_database(settings):
    from retailq.db import Database
    from retailq.errors import ServiceError
    from sqlalchemy import text
    if (settings.mysql_port != 3307 or settings.mysql_database != 'retailq_test'
            or settings.mysql_host not in ('127.0.0.1','localhost','::1')):
        raise ServiceError('knowledge_scope_invalid',409,'仅允许重置本机3307/retailq_test隔离测试库。')
    database = Database(settings)
    try:
        database.ping()
    except Exception:
        database.close()
        pytest.fail('Start isolated MySQL: docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql', pytrace=False)
    # Bootstrap test data from the supplied SQL, not production ORM create_all.
    from sqlalchemy import inspect
    import re
    with database.engine.begin() as connection:
        ddl = re.sub(r'(?m)^--.*$', '', Path('db/init/03-knowledge.sql').read_text(encoding='utf-8'))
        for statement in ddl.split(';'):
            match = re.search(r'CREATE TABLE (\w+)', statement)
            if statement.strip() and (not match or not inspect(connection).has_table(match[1])):
                connection.exec_driver_sql(statement)
    with database.sessions.begin() as session:
        session.execute(text('UPDATE knowledge_chunks SET prev_chunk_id=NULL, next_chunk_id=NULL'))
        for name in ('qa_extraction_staging', 'knowledge_chunks'):
            session.execute(text(f'DELETE FROM {name}'))
        for name in ('tickets', 'messages', 'faq', 'conversations'):
            session.execute(text(f'DELETE FROM {name}'))
        for statement in Path('db/init/02-seed.sql').read_text(encoding='utf-8').split(';'):
            if statement.strip():
                session.connection().exec_driver_sql(statement)
    return database


@pytest.fixture
def test_database():
    database = reset_isolated_database(Settings(mysql_port=3307, mysql_database='retailq_test'))
    try:
        yield database
    finally:
        database.close()
