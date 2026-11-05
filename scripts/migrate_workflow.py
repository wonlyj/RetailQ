"""Explicit non-destructive workflow metadata migration."""
import argparse
from hashlib import sha256
from pathlib import Path
import re

from sqlalchemy import inspect
from retailq.config import Settings
from retailq.db import Database
from retailq.errors import ServiceError
from retailq.workflow.models import WorkflowTurn,WorkflowAction

DDL_SHA256 = 'd28c5d91f37c3e599247fc2ce879b9dced82b19e7b649083792d748ffb1fa319'


def validate_scope(settings,scope):
    expected = {'test':(3307,'retailq_test'),'demo':(3306,'retailq')}.get(scope)
    if (settings.mysql_host not in ('127.0.0.1','localhost','::1') or
            (settings.mysql_port,settings.mysql_database)!=expected):
        raise ServiceError('workflow_scope_invalid',409,'数据库与迁移scope不匹配，未修改。')


def verify_workflow(connection,table):
    inspector = inspect(connection)
    actual = {c['name']:c for c in inspector.get_columns(table.name)}
    valid = set(actual)==set(table.columns.keys())
    def default(value): return str('' if value is None else value).strip("'\"").lower().replace('()','')
    for column in table.columns:
        found = actual.get(column.name)
        if found is None:
            valid = False
            continue
        valid &= found['type'].compile(dialect=connection.dialect).lower()==column.type.compile(dialect=connection.dialect).lower()
        valid &= found['nullable']==column.nullable
        valid &= default(found.get('default'))==default(column.server_default.arg if column.server_default else None)
    valid &= inspector.get_pk_constraint(table.name)['constrained_columns']==[c.name for c in table.primary_key]
    indexes = {(tuple(i['column_names']),bool(i['unique'])) for i in inspector.get_indexes(table.name)}
    for index in table.indexes: valid &= (tuple(c.name for c in index.columns),bool(index.unique)) in indexes
    for column in table.columns:
        if column.unique: valid &= ((column.name,),True) in indexes
    actual_fks = {(tuple(f['constrained_columns']),f['referred_table'],tuple(f['referred_columns'])) for f in inspector.get_foreign_keys(table.name)}
    expected_fks = {(tuple(c.name for c in f.columns),f.referred_table.name,tuple(e.column.name for e in f.elements)) for f in table.foreign_key_constraints}
    valid &= actual_fks==expected_fks
    from sqlalchemy import text
    collations = dict(connection.execute(text('SELECT COLUMN_NAME,COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name'),{'name':table.name}).all())
    for column in table.columns:
        if getattr(column.type,'collation',None): valid &= collations[column.name]==column.type.collation
    if not valid: raise ServiceError('workflow_schema_mismatch',409,'工作流表结构不匹配，请核查迁移。')


def migrate_workflow(database,ddl_path:Path):
    content = ddl_path.read_bytes()
    if sha256(content).hexdigest()!=DDL_SHA256: raise ValueError('workflow DDL checksum mismatch')
    tables = {t.name:t for t in (WorkflowTurn.__table__,WorkflowAction.__table__)}
    ddl = re.sub(r'(?m)^--.*$','',content.decode('utf-8'))
    with database.engine.begin() as connection:
        for table in tables.values():
            if inspect(connection).has_table(table.name): verify_workflow(connection,table)
        for statement in ddl.split(';'):
            statement = statement.strip()
            if not statement: continue
            match = re.match(r'CREATE TABLE (\w+)',statement)
            if match and match[1] in tables:
                if not inspect(connection).has_table(match[1]): connection.exec_driver_sql(statement)
            elif statement.upper()=='SET NAMES UTF8MB4': connection.exec_driver_sql(statement)
            else: raise ValueError('unexpected workflow DDL statement')
        for table in tables.values(): verify_workflow(connection,table)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',choices=('test','demo'),required=True)
    args = parser.parse_args(argv)
    settings = Settings()
    validate_scope(settings,args.scope)
    database = Database(settings)
    try:
        migrate_workflow(database,Path(__file__).resolve().parents[1]/'db/init/05-workflow.sql')
        print('Workflow metadata migration verified.')
    finally: database.close()


if __name__=='__main__': main()
