import subprocess
import sys
import threading
import errno
import os
from pathlib import Path

import pytest

from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.errors import ServiceError


def identity(root):
    return RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root / 'knowledge')


def worker(root):
    return subprocess.Popen([sys.executable, '-m', 'tests.support.coordination_worker', str(root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cross_process_maintenance_has_one_owner(tmp_path):
    first = worker(tmp_path)
    try:
        first_result = first.stdout.readline().strip()
        second = worker(tmp_path)
        try:
            second_result = second.stdout.readline().strip()
            assert sum(value == 'acquired' for value in (first_result, second_result)) == 1
            assert second_result == 'busy'
            assert second.wait(timeout=10) == 0
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=10)
    finally:
        first.kill()
        first.wait(timeout=10)
    third = worker(tmp_path)
    try:
        assert third.stdout.readline().strip() == 'acquired'
        third.stdin.write('\n')
        third.stdin.flush()
        assert third.wait(timeout=10) == 0
    finally:
        if third.poll() is None:
            third.kill()
            third.wait(timeout=10)


def test_identity_binding_and_close_are_safe(tmp_path):
    coordinator = Coordinator(tmp_path / 'locks', identity(tmp_path))
    lease = coordinator.try_acquire('maintenance')
    assert lease is not None
    assert Coordinator(tmp_path / 'locks', identity(tmp_path)).try_acquire('maintenance') is None
    lease.close()
    lease.close()
    different = RuntimeIdentity('localhost', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path / 'other')
    with pytest.raises(ServiceError):
        Coordinator(tmp_path / 'locks', different)

    other_database = RuntimeIdentity('127.0.0.1', 3307, 'different_db',
                                     'http://127.0.0.1:19531', tmp_path / 'knowledge')
    with pytest.raises(ServiceError):
        Coordinator(tmp_path / 'locks', other_database)


def test_audit_lock_reentrant_and_serializes_threads(tmp_path):
    first = Coordinator(tmp_path / 'locks', identity(tmp_path))
    second = Coordinator(tmp_path / 'locks', identity(tmp_path))
    entered = threading.Event()
    with first.short_lock('audit'):
        with first.short_lock('audit'):
            thread = threading.Thread(target=lambda: enter(second, entered))
            thread.start()
            assert not entered.wait(0.1)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert entered.is_set()


def enter(coordinator, entered):
    with coordinator.short_lock('audit'):
        entered.set()


def test_invalid_scope_and_symlink_escape_are_rejected(tmp_path):
    coordinator = Coordinator(tmp_path / 'locks', identity(tmp_path))
    with pytest.raises(ValueError):
        coordinator.try_acquire('../escape')


def test_lock_file_symlink_outside_root_is_rejected(tmp_path):
    coordinator = Coordinator(tmp_path / 'locks', identity(tmp_path))
    lock_path = coordinator._path('maintenance')
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep', encoding='utf-8')
    try:
        lock_path.symlink_to(outside)
    except OSError:
        pytest.skip('Windows symlink creation is unavailable')
    with pytest.raises(ServiceError):
        coordinator.try_acquire('maintenance')
    assert outside.read_text(encoding='utf-8') == 'keep'


def test_audit_lock_prevents_lost_updates_across_processes(tmp_path):
    processes = [subprocess.Popen([sys.executable, '-m', 'tests.support.coordination_worker',
                                  str(tmp_path), 'counter'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
    try:
        for process in processes:
            assert process.wait(timeout=30) == 0, process.stderr.read().decode(errors='replace')
        assert (tmp_path / 'counter.txt').read_text() == '60'
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_settings_paths_are_independent_of_current_working_directory(tmp_path, monkeypatch):
    import retailq.coordination as module
    from retailq.config import Settings

    captured = []
    monkeypatch.setattr(Coordinator, '__init__', lambda self, root, identity: captured.append((root, identity)))
    monkeypatch.chdir(tmp_path)
    Coordinator.for_settings(Settings(_env_file=None, knowledge_data_dir='artifacts/knowledge'))
    project = Path(module.__file__).resolve().parents[1]
    assert captured[0][1].root == project / 'artifacts' / 'knowledge'


@pytest.mark.skipif(os.name != 'nt', reason='tests Windows locking error classification')
def test_os_lock_io_errors_are_not_reported_as_contention(tmp_path, monkeypatch):
    import msvcrt

    coordinator = Coordinator(tmp_path / 'locks', identity(tmp_path))
    def fail(*args):
        raise OSError(errno.EIO, 'I/O failure')
    monkeypatch.setattr(msvcrt, 'locking', fail)
    with pytest.raises(OSError) as exc:
        coordinator.try_acquire('maintenance')
    assert exc.value.errno == errno.EIO
