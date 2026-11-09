"""Explicit chapter-six enum expansion; no row rewriting or schema recreation."""
import argparse
from hashlib import sha256
from pathlib import Path
from sqlalchemy import inspect
from retailq.config import Settings
from retailq.db import Database
from retailq.errors import ServiceError
from scripts.migrate_workflow import validate_scope

DDL_SHA256='7325b2962941c9a26969694f81307b759809faf5360c24b2557764fcfdbe4985'
OLD=('售后','投诉','咨询');NEW=(*OLD,'退款')


def migrate_router(database,ddl_path):
    if sha256(ddl_path.read_bytes()).hexdigest()!=DDL_SHA256:raise ValueError('router DDL checksum mismatch')
    with database.engine.begin() as connection:
        column=next((c for c in inspect(connection).get_columns('tickets') if c['name']=='ticket_type'),None)
        values=tuple(getattr(column['type'],'enums',())) if column else ()
        if not column or column['nullable'] or column.get('default') is not None or values not in (OLD,NEW):
            raise ServiceError('router_schema_mismatch',409,'工单原结构不匹配，未执行迁移。')
        if values==OLD:
            connection.exec_driver_sql('SET NAMES utf8mb4')
            connection.exec_driver_sql("ALTER TABLE tickets MODIFY COLUMN ticket_type ENUM('售后','投诉','咨询','退款') NOT NULL")
        actual=next(c for c in inspect(connection).get_columns('tickets') if c['name']=='ticket_type')
        if tuple(actual['type'].enums)!=NEW or actual['nullable']:raise ServiceError('router_schema_mismatch',409,'退款工单枚举未正确保存。')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--scope',choices=('test','acceptance','demo'),required=True)
    args=parser.parse_args(argv);settings=Settings();validate_scope(settings,args.scope)
    database=Database(settings)
    try:migrate_router(database,Path(__file__).resolve().parents[1]/'db/init/06-ticket-type.sql')
    finally:database.close()
    print('Chapter six ticket enum migration verified.')


if __name__=='__main__':main()
