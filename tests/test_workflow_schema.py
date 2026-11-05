from pathlib import Path
import pytest
from sqlalchemy import inspect
from retailq.errors import ServiceError

pytestmark = pytest.mark.mysql


def test_explicit_workflow_migration_is_repeatable_and_matches_schema(test_database):
    from scripts.migrate_workflow import migrate_workflow
    migrate_workflow(test_database,Path('db/init/05-workflow.sql'))
    migrate_workflow(test_database,Path('db/init/05-workflow.sql'))
    inspector = inspect(test_database.engine)
    assert inspector.get_pk_constraint('workflow_turns')['constrained_columns']==['turn_id']
    assert inspector.get_pk_constraint('workflow_actions')['constrained_columns']==['action_id']
    assert not next(c for c in inspector.get_columns('workflow_turns') if c['name']=='turn_id').get('autoincrement')


def test_scope_guard_cannot_migrate_unrelated_database():
    from scripts.migrate_workflow import validate_scope
    from retailq.config import Settings
    with pytest.raises(ServiceError): validate_scope(Settings(_env_file=None,mysql_port=3308),'test')
