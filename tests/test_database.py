import pytest
from pydantic import ValidationError

from retailq.config import Settings


def test_database_password_special_characters():
    password = 'test@:/#%secret'
    settings = Settings(_env_file=None, mysql_password=password)
    assert settings.is_database_configured
    assert settings.database_url.password == password
    assert password not in repr(settings)
    assert settings.database_url.drivername == 'mysql+pymysql'
    assert settings.tool_timeout_seconds == 5
    assert settings.tool_read_retries == 1


def test_database_empty_config_and_invalid_bounds():
    assert not Settings(_env_file=None).is_database_configured
    for kwargs in ({'mysql_port': 0}, {'tool_timeout_seconds': 0}, {'tool_read_retries': 2}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


def test_mysql_mapping_names():
    from retailq.models import Base
    assert set(Base.metadata.tables) == {'faq', 'conversations', 'messages', 'tickets'}


@pytest.mark.mysql
def test_mysql_mapping_matches_supplied_ddl(test_database):
    from sqlalchemy import inspect
    from retailq.models import Base
    inspector = inspect(test_database.engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        actual = {c['name']: c for c in inspector.get_columns(name)}
        assert set(actual) == set(table.columns.keys())
        for column in table.columns:
            assert actual[column.name]['nullable'] == column.nullable
        if 'conversation_id' in actual:
            assert actual['conversation_id']['type'].unsigned
    messages = {c['name']: c for c in inspector.get_columns('messages')}
    assert messages['role']['type'].enums == ['user', 'assistant', 'tool']
    assert str(messages['tool_calls']['type']) == 'JSON'
    assert inspector.get_foreign_keys('messages')[0]['referred_table'] == 'conversations'
    assert inspector.get_foreign_keys('tickets')[0]['referred_table'] == 'conversations'


@pytest.mark.mysql
def test_seed_four_tables_and_faq_gap(test_database):
    from sqlalchemy import text
    with test_database.sessions() as session:
        for name in ('faq', 'messages', 'conversations', 'tickets'):
            assert session.scalar(text(f'SELECT COUNT(*) FROM {name}')) > 0
        assert session.scalar(text("SELECT COUNT(*) FROM faq WHERE CONCAT(question, answer, category) LIKE '%邮费%'")) == 0
        calls = session.execute(text("SELECT tool_call_id FROM messages WHERE role='tool'")).scalars().all()
        assert calls == ['seed_call_1']
