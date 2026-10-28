"""Owned subprocess kill/reopen acceptance; never touches the demo database."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from retailq.config import Settings
from retailq.errors import ServiceError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'artifacts/verification/ch03-review'
SCENARIOS = tuple((stage, decision) for stage in (
    'after_decision_persisted', 'after_prepared_before_sql_commit',
    'after_sql_commit_before_applied') for decision in ('approve', 'reject')) + (
    ('after_milvus_upsert_before_mysql_done', 'approve'), ('queued_before_worker', 'approve'),
    ('corrupt_run_seal', 'approve'), ('seal_active_worker', 'approve'))


def test_settings(**overrides):
    local = Settings()
    return Settings(_env_file=None, mysql_host='127.0.0.1', mysql_port=3307,
        mysql_database='retailq_test', mysql_user=local.mysql_user, mysql_password=local.mysql_password,
        knowledge_enabled=True, knowledge_schedule_enabled=False, **overrides)


def validate_recovery_scope(settings, output, *, existing=False):
    output = Path(output).resolve()
    if (settings.mysql_host not in ('localhost', '127.0.0.1', '::1') or settings.mysql_port != 3307
            or settings.mysql_database != 'retailq_test' or not output.is_relative_to(EVIDENCE.resolve())
            or output == EVIDENCE.resolve() or not output.name.startswith('recovery-')
            or (output.exists() and not existing)):
        raise ServiceError('knowledge_scope_invalid', 409,
            '恢复验收仅允许本机3307/retailq_test和全新artifacts/verification/ch03-review/recovery-*目录。')
    return output


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def wait_file(path, process, *, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        if process.poll() is not None:
            raise RuntimeError(f'worker exited {process.returncode} before {path.name}; see worker log')
        time.sleep(.05)
    raise TimeoutError(f'worker checkpoint timeout: {path.name}')


def sql_snapshot(config):
    from retailq.db import Database
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.review_store import ReviewStore
    from datetime import datetime, timezone
    database = Database(test_settings())
    try:
        knowledge = KnowledgeRepository(database)
        store = ReviewStore(Path(config['case']) / 'knowledge', clock=lambda: datetime.now(timezone.utc))
        try:
            run = store.get_run(config['run_id'])
            rows = knowledge.staging([b.batch_no for b in run.batches])
        except ServiceError:
            rows = []  # Corrupt/queued scenarios have no invented batch membership.
        return {'new_chunk_ids': [str(r.id) for r in knowledge.all_chunks() if str(r.id) not in config['baseline_ids']],
                'staging_statuses': [r.status for r in rows],
                'staging': [{'id':str(r.id), 'status':r.status} for r in rows]}
    finally:
        database.close()


def run_case(output, stage, decision, embedding_mode):
    """All child processes are owned Popen objects, never discovered by PID scan."""
    output = validate_recovery_scope(test_settings(), output, existing=True)
    if (stage, decision) not in SCENARIOS or embedding_mode not in ('deterministic', 'live'):
        raise ValueError('unknown recovery scenario or embedding mode')
    case = output / f'{stage}-{decision}'
    case.mkdir()  # Never reuse another run's files or model identity.
    config = {'case':str(case), 'stage':stage, 'decision':decision, 'embedding_mode':embedding_mode,
              'run_id':uuid.uuid4().hex, 'decision_id':uuid.uuid4().hex, 'seal_id':uuid.uuid4().hex}
    write_json(case / 'request.json', config)
    commands = []
    report = {'stage':stage, 'decision':decision, 'embedding_mode':embedding_mode,
              'chat_mode':'fixture', 'commands':commands, 'passed':False}
    if os.name == 'nt':
        report['process_environment'] = {'__PYVENV_LAUNCHER__':sys.executable}
    processes = []
    def spawn(mode):
        # Windows venv python.exe is a redirector. Launch its actual interpreter
        # with CPython's venv launcher variable so Popen.pid is the worker itself.
        executable = sys._base_executable if os.name == 'nt' else sys.executable
        environment = dict(os.environ)
        if os.name == 'nt':
            environment['__PYVENV_LAUNCHER__'] = sys.executable
        command = [executable, '-m', 'tests.support.review_worker', '--request', str(case / 'request.json'),
                   '--mode', mode]
        commands.append(command)
        log = (case / f'{mode}.log').open('w', encoding='utf-8')
        try:
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=environment)
        finally:
            log.close()
        processes.append(child)
        return child
    try:
        first = spawn('interrupt')
        ready = wait_file(case / 'ready.json', first)
        if ready['pid'] != first.pid or ready['stage'] != stage or ready['operation_id'] != (
                config['run_id'] if stage == 'queued_before_worker' else config['decision_id']):
            raise RuntimeError('checkpoint does not belong to this owned process/operation')
        config = json.loads((case / 'request.json').read_text(encoding='utf-8'))
        report.update(ready=ready, before=sql_snapshot(config), embedding=ready['embedding'],
                      baseline_chunk_ids=config['baseline_ids'], source_ids=config['source_ids'])
        first.kill()
        report['killed_pid'] = first.pid
        report['wait_returncode'] = first.wait(timeout=20)
        # Reopen is strictly after wait(), including on Windows file locks.
        second = spawn('recover')
        code = second.wait(timeout=120)
        if code:
            raise RuntimeError(f'recovery worker exited {code}; see recover.log')
        recovered = json.loads((case / 'recovered.json').read_text(encoding='utf-8'))
        report['recovered'] = recovered
        report['passed'] = recovered['passed']
        if not recovered['passed']:
            report['error'] = 'recovered evidence did not satisfy every invariant'
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=20)
        write_json(case / 'report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=['test'], required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--embedding-mode', choices=['deterministic', 'live'], required=True)
    args = parser.parse_args(argv)
    try:
        output = validate_recovery_scope(test_settings(), args.output)
        if args.embedding_mode == 'live' and not Settings().embedding_api_key:
            raise ServiceError('embedding_not_configured', 503, 'live需要本地EMBEDDING_API_KEY。')
        output.mkdir(parents=True)
    except ServiceError as exc:
        print(f'{exc.code}: {exc.public_message}')
        return 2
    results = []
    for stage, decision in SCENARIOS:
        report = run_case(output, stage, decision, args.embedding_mode)
        results.append(report)
        print(f'{stage}/{decision}: {"PASS" if report["passed"] else "FAIL"}', flush=True)
    write_json(output / 'summary.json', {'embedding_mode':args.embedding_mode, 'chat_mode':'fixture',
        'passed':all(r['passed'] for r in results), 'cases':results})
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
