"""Apply the supplied chapter-four DDL without rewriting existing tables."""
from hashlib import sha256
from pathlib import Path
import re

from sqlalchemy import inspect

from retailq.errors import ServiceError
from retailq.knowledge.rag_models import FaithCase, LowConfidenceQuestion
from scripts.migrate_knowledge import verify

DDL_SHA256 = '8840159feb482e325db6b052e99eb646e1758d7bfe3489d7ab184e535b0df118'


def verify_rag_table(connection, table) -> None:
    verify(connection, table)
    actual = {(index['name'], tuple(index['column_names']), bool(index.get('unique')))
              for index in inspect(connection).get_indexes(table.name)}
    expected = {(index.name, tuple(column.name for column in index.columns), bool(index.unique))
                for index in table.indexes}
    if not expected <= actual:
        raise ServiceError('knowledge_schema_mismatch', 409, f'知识表 {table.name} 索引结构不符合原始DDL。')


def migrate_rag(database, ddl_path: Path) -> None:
    content = ddl_path.read_bytes()
    if sha256(content).hexdigest() != DDL_SHA256:
        raise ValueError('chapter four DDL checksum mismatch')
    ddl = re.sub(r'(?m)^--.*$', '', content.decode('utf-8'))
    tables = {table.name: table for table in (LowConfidenceQuestion.__table__, FaithCase.__table__)}
    with database.engine.begin() as connection:
        for table in tables.values():
            if inspect(connection).has_table(table.name):
                verify_rag_table(connection, table)
        # Semicolons inside quoted COMMENT strings are not statement separators.
        for statement in re.split(r";(?=(?:[^']*'[^']*')*[^']*$)", ddl):
            if not statement.strip():
                continue
            match = re.search(r'CREATE TABLE (\w+)', statement)
            if match and match[1] in tables:
                if not inspect(connection).has_table(match[1]):
                    connection.exec_driver_sql(statement)
            elif statement.strip().upper() == 'SET NAMES UTF8MB4':
                connection.exec_driver_sql(statement)
            else:
                raise ValueError('unexpected chapter four DDL statement')
        for table in tables.values():
            verify_rag_table(connection, table)
