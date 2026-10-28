"""Real OS process death must not turn a plan into a confirmed publication."""
from pathlib import Path
import uuid

import pytest

from retailq.config import Settings
from retailq.errors import ServiceError


def recovery_module():
    import importlib.util
    assert importlib.util.find_spec('scripts.verify_knowledge_review_recovery'), 'recovery runner is missing'
    from scripts import verify_knowledge_review_recovery
    return verify_knowledge_review_recovery


def test_recovery_refuses_demo_database_and_knowledge_directory():
    runner = recovery_module()
    with pytest.raises(ServiceError):
        runner.validate_recovery_scope(Settings(_env_file=None, mysql_port=3306,
            mysql_database='retailq'), Path('artifacts/knowledge'))
    settings = runner.test_settings()
    for output in (runner.EVIDENCE, runner.ROOT / 'artifacts/knowledge', runner.EVIDENCE / '../escape'):
        with pytest.raises(ServiceError):
            runner.validate_recovery_scope(settings, output)


@pytest.fixture
def recovery_case(test_database):
    runner = recovery_module()
    output = runner.EVIDENCE / ('recovery-pytest-' + uuid.uuid4().hex)
    runner.validate_recovery_scope(runner.test_settings(), output)
    output.mkdir(parents=True)
    return runner, output


@pytest.mark.mysql
@pytest.mark.lite
@pytest.mark.parametrize('stage,decision', [
    ('after_decision_persisted', 'approve'), ('after_decision_persisted', 'reject'),
    ('after_prepared_before_sql_commit', 'approve'), ('after_prepared_before_sql_commit', 'reject'),
    ('after_sql_commit_before_applied', 'approve'), ('after_sql_commit_before_applied', 'reject'),
    ('after_milvus_upsert_before_mysql_done', 'approve'),
])
def test_parent_waits_for_worker_exit_before_reopen(recovery_case, stage, decision):
    runner, output = recovery_case
    report = runner.run_case(output, stage, decision, 'deterministic')
    assert report['passed'], report
    assert report['killed_pid'] == report['ready']['pid']
    assert report['wait_returncode'] is not None
    assert report['recovered']['pid'] != report['killed_pid']
    assert report['embedding']['model'] == 'deterministic-test'
    assert report['chat_mode'] == 'fixture'
    after = report['recovered']
    assert after['confirmed'] and after['all_members_match'] and after['fingerprint_matches']
    assert after['new_chunk_count'] == (1 if decision == 'approve' else 0)
    assert after['vector_count'] == (1 if decision == 'approve' else 0)
    assert after['pending_count'] == 0
    if stage == 'after_prepared_before_sql_commit':
        assert set(report['before']['staging_statuses']) == {'extracted'}
        assert report['before']['new_chunk_ids'] == []
    if stage in ('after_sql_commit_before_applied', 'after_milvus_upsert_before_mysql_done'):
        assert after['target_id'] == report['ready']['planned_id']


@pytest.mark.mysql
@pytest.mark.lite
def test_killed_committed_group_has_same_confirmed_target_after_restart(recovery_case):
    runner, output = recovery_case
    report = runner.run_case(output, 'after_sql_commit_before_applied', 'approve', 'deterministic')
    assert report['passed'], report
    assert report['before']['new_chunk_ids'] == [report['ready']['planned_id']]
    assert report['recovered']['target_id'] == report['ready']['planned_id']
    assert {m['reason'] for m in report['recovered']['dispositions']} == {'new', 'same_group'}


@pytest.mark.mysql
@pytest.mark.lite
def test_killed_prepared_transaction_not_misread_as_applied(recovery_case):
    runner, output = recovery_case
    report = runner.run_case(output, 'after_prepared_before_sql_commit', 'approve', 'deterministic')
    assert report['passed'], report
    assert report['ready']['planned_id'] is not None
    assert report['before']['new_chunk_ids'] == []
    assert report['before']['staging_statuses'] == ['extracted', 'extracted']
    assert report['recovered']['target_id'] != report['ready']['planned_id']


@pytest.mark.mysql
@pytest.mark.lite
@pytest.mark.parametrize('stage', ['queued_before_worker', 'corrupt_run_seal', 'seal_active_worker'])
def test_sealed_queued_command_stays_sealed_after_restart(recovery_case, stage):
    runner, output = recovery_case
    report = runner.run_case(output, stage, 'approve', 'deterministic')
    assert report['passed'], report
    assert report['recovered']['new_chunk_count'] == 0
    if stage == 'queued_before_worker':
        assert report['recovered']['phase'] == 'waiting_review'
    else:
        assert report['recovered']['sealed'] and report['recovered']['revival_blocked']
    if stage == 'seal_active_worker':
        assert report['ready']['seal_queued'] and not report['ready']['seal_marker']
