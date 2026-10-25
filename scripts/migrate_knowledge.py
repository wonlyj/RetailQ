"""Apply only the supplied knowledge DDL; existing tables are never rewritten."""
from pathlib import Path
import re

from sqlalchemy import inspect

from retailq.db import Database
from retailq.errors import ServiceError
from retailq.knowledge.models import KnowledgeChunk, QAStaging


def verify(connection,table):
    inspector=inspect(connection)
    columns={c['name']:c for c in inspector.get_columns(table.name)}
    valid=set(columns)==set(table.columns.keys())
    for name,column in table.columns.items():
        actual=columns.get(name)
        if actual is None:
            valid=False
            continue
        expected_type=column.type.compile(dialect=connection.dialect).lower()
        actual_type=actual['type'].compile(dialect=connection.dialect).lower()
        valid &= expected_type==actual_type and column.nullable==actual['nullable']
        def normalized_default(value):
            return str('' if value is None else value).strip("'\"").lower().replace('()','')
        expected_default=column.server_default.arg if column.server_default is not None else None
        valid &= normalized_default(actual.get('default'))==normalized_default(expected_default)
        if column.primary_key:
            valid &= bool(actual.get('autoincrement'))
    expected_indexes={(index.name,tuple(c.name for c in index.columns)) for index in table.indexes}
    actual_indexes={(i['name'],tuple(i['column_names'])) for i in inspector.get_indexes(table.name)}
    valid &= expected_indexes <= actual_indexes
    foreign={(tuple(f['constrained_columns']),f['referred_table'],tuple(f['referred_columns']),f['options'].get('ondelete')) for f in inspector.get_foreign_keys(table.name)}
    expected={(tuple(c.name for c in fk.columns),fk.referred_table.name,tuple(e.column.name for e in fk.elements),fk.ondelete) for fk in table.foreign_key_constraints}
    valid &= foreign == expected
    valid &= inspector.get_pk_constraint(table.name)['constrained_columns']==['id']
    if not valid:
        raise ServiceError('knowledge_schema_mismatch',409,f'知识表 {table.name} 结构与原始DDL不一致，请人工核查。')


def migrate(database: Database,ddl_path: Path) -> None:
    ddl=re.sub(r'(?m)^--.*$','',ddl_path.read_text(encoding='utf-8'))
    tables={t.name:t for t in [KnowledgeChunk.__table__,QAStaging.__table__]}
    with database.engine.begin() as connection:
        # Validate every existing table before applying any missing CREATE.
        for table in tables.values():
            if inspect(connection).has_table(table.name):
                verify(connection,table)
        for statement in ddl.split(';'):
            if not statement.strip():
                continue
            match=re.search(r'CREATE TABLE (\w+)',statement)
            if match:
                if match[1] not in tables:
                    raise ValueError('unexpected table in supplied knowledge DDL')
                if not inspect(connection).has_table(match[1]):
                    connection.exec_driver_sql(statement)
            elif statement.strip().upper()=='SET NAMES UTF8MB4':
                connection.exec_driver_sql(statement)
            else:
                raise ValueError('unexpected statement in supplied knowledge DDL')
        for table in tables.values():
            verify(connection,table)
