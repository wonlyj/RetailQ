from pathlib import Path
import pytest
from sqlalchemy import text,inspect

pytestmark=pytest.mark.mysql


def test_enum_migration_preserves_old_rows_and_is_repeatable(test_database):
    from scripts.migrate_router import migrate_router
    with test_database.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE tickets MODIFY COLUMN ticket_type ENUM('售后','投诉','咨询') NOT NULL")
        connection.exec_driver_sql("INSERT INTO conversations(user_id) VALUES ('migration-alice')")
        cid=connection.scalar(text('SELECT LAST_INSERT_ID()'))
        connection.execute(text("INSERT INTO tickets(ticket_no,conversation_id,description,ticket_type) VALUES ('MIG-OLD',:cid,'旧记录','咨询')"),{'cid':cid})
        old=connection.execute(text("SELECT * FROM tickets WHERE ticket_no='MIG-OLD'")).one()
    for _ in range(2):migrate_router(test_database,Path('db/init/06-ticket-type.sql'))
    with test_database.engine.connect() as connection:
        column=next(c for c in inspect(connection).get_columns('tickets') if c['name']=='ticket_type')
        assert column['type'].enums==['售后','投诉','咨询','退款']
        assert connection.execute(text("SELECT * FROM tickets WHERE ticket_no='MIG-OLD'")).one()==old
