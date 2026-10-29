"""Scoped same-host process leases for one configured database and corpus."""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

from retailq.errors import ServiceError

_SCOPES = frozenset({'maintenance', 'audit', 'conversation', 'scheduler', 'evaluation'})
_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LOCAL = threading.local()


def _host(host: str) -> str:
    host = host.strip().lower()
    return '127.0.0.1' if host in ('localhost', '127.0.0.1', '::1') else host


@dataclass(frozen=True)
class RuntimeIdentity:
    mysql_host: str
    mysql_port: int
    mysql_database: str
    milvus_uri: str
    root: Path

    def normalized(self) -> dict:
        parsed = urlsplit(self.milvus_uri)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid Milvus URI')
        return {'mysql_host': _host(self.mysql_host), 'mysql_port': self.mysql_port,
                'mysql_database': self.mysql_database, 'milvus_scheme': parsed.scheme,
                'milvus_host': _host(parsed.hostname), 'milvus_port': parsed.port,
                'knowledge_root': os.path.normcase(str(Path(self.root).resolve()))}


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _try_os_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProcessLease:
    def __init__(self, handle, thread_lock):
        self._handle, self._thread_lock = handle, thread_lock
        self._closing = threading.Lock()

    def close(self) -> None:
        with self._closing:
            if self._handle is None:
                return
            try:
                _unlock(self._handle)
            finally:
                self._handle.close()
                self._handle = None
                self._thread_lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Coordinator:
    def __init__(self, root: Path, identity: RuntimeIdentity):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = identity.normalized()
        database = json.dumps([self.identity['mysql_host'], self.identity['mysql_port'],
                               self.identity['mysql_database']], ensure_ascii=False, separators=(',', ':'))
        self._database_key = sha256(database.encode('utf-8')).hexdigest()
        with self._hold(self._safe('bindings.lock')):
            self._bind()

    @classmethod
    def for_settings(cls, settings):
        project = Path(__file__).resolve().parents[1]
        root = (project / 'artifacts' / 'runtime' / 'coordination').resolve()
        if not root.is_relative_to(project):
            raise ServiceError('coordination_path_invalid', 409, '协调目录超出项目范围。')
        knowledge_root = Path(settings.knowledge_data_dir)
        if not knowledge_root.is_absolute():
            knowledge_root = project / knowledge_root
        return cls(root, RuntimeIdentity(settings.mysql_host, settings.mysql_port, settings.mysql_database,
                                         settings.milvus_uri, knowledge_root))

    def _safe(self, name: str) -> Path:
        path = self.root / name
        if not path.resolve().is_relative_to(self.root):
            raise ServiceError('coordination_path_invalid', 409, '协调文件路径无效。')
        return path

    def _bind(self) -> None:
        root_key = sha256(self.identity['knowledge_root'].encode('utf-8')).hexdigest()
        paths = (self._safe('binding-' + self._database_key + '.json'),
                 self._safe('corpus-' + root_key + '.json'))
        for path in paths:
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise ServiceError('coordination_identity_invalid', 409, '协调身份文件损坏，请核查。') from None
                if existing != self.identity:
                    raise ServiceError('coordination_identity_mismatch', 409, '数据库或知识目录已绑定不同的协调身份。')
        for path in paths:
            if path.exists():
                continue
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(self.identity, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _path(self, scope: str, key: str = '') -> Path:
        if scope not in _SCOPES or not isinstance(key, str):
            raise ValueError('invalid coordination scope or key')
        digest = sha256((self._database_key + '\0' + scope + '\0' + key).encode('utf-8')).hexdigest()
        return self._safe(scope + '-' + digest + '.lock')

    def try_acquire(self, scope: str, key: str = '') -> ProcessLease | None:
        path = self._path(scope, key)
        thread_lock = _lock_for(path)
        if not thread_lock.acquire(blocking=False):
            return None
        handle = None
        try:
            handle = path.open('a+b')
            if not _try_os_lock(handle):
                handle.close()
                thread_lock.release()
                return None
            return ProcessLease(handle, thread_lock)
        except BaseException:
            if handle is not None:
                handle.close()
            thread_lock.release()
            raise

    @contextmanager
    def short_lock(self, scope: str):
        with self._hold(self._path(scope)):
            yield

    @contextmanager
    def _hold(self, path: Path):
        key = os.path.normcase(str(path))
        held = getattr(_LOCAL, 'held', None)
        if held is None:
            held = _LOCAL.held = {}
        if key in held:
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        thread_lock = _lock_for(path)
        thread_lock.acquire()
        handle = None
        try:
            handle = path.open('a+b')
            while not _try_os_lock(handle):
                time.sleep(0.01)
            held[key] = 1
            try:
                yield
            finally:
                del held[key]
                _unlock(handle)
        finally:
            if handle is not None:
                handle.close()
            thread_lock.release()
