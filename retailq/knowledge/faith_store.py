"""Durable single active evaluation identity with an OS-backed lease."""
import json
from pathlib import Path
import re

from retailq.errors import ServiceError
from retailq.knowledge.corpus import _write


def _fail():
    return ServiceError('faith_run_conflict', 409, '评估轮次状态冲突，请恢复或封存已有轮次。')


class FaithStore:
    def __init__(self, root, coordinator):
        self.root, self.coordinator = Path(root).resolve(), coordinator
        self.root.mkdir(parents=True, exist_ok=True)
        self.lease, self.run_id, self.verify = None, None, None

    @property
    def active_path(self): return self.root / 'active.json'

    def _read(self):
        try:
            value = json.loads(self.active_path.read_text(encoding='utf-8'))
            if set(value) != {'run_id', 'identity', 'expected'} or not isinstance(value['expected'], list): raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            raise _fail() from None

    def _acquire(self):
        if self.lease is not None: raise _fail()
        self.lease = self.coordinator.try_acquire('evaluation')
        if self.lease is None: raise _fail()

    def _recover_terminal(self):
        if not self.active_path.exists(): return
        active = self._read()
        run_id = active['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id): raise _fail()
        path = self.root / (run_id + '.json')
        if not path.exists(): return
        try:
            terminal = json.loads(path.read_text(encoding='utf-8'))
            if (terminal.get('status') not in ('completed', 'sealed') or
                    any(terminal.get(k) != active[k] for k in ('run_id', 'identity', 'expected'))):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise _fail() from None
        self.active_path.unlink()

    def begin(self, run_id, run_identity):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id) or not isinstance(run_identity, dict):
            raise ValueError('invalid run identity')
        self._acquire()
        try:
            self._recover_terminal()
            if self.active_path.exists() or (self.root / (run_id + '.json')).exists(): raise _fail()
            _write(self.active_path, {'run_id':run_id, 'identity':run_identity, 'expected':[]})
            self.run_id = run_id
        except BaseException:
            self.close()
            raise

    def resume(self, run_id):
        self._acquire()
        try:
            self._recover_terminal()
            if self._read()['run_id'] != run_id: raise _fail()
            self.run_id = run_id
        except BaseException:
            self.close()
            raise

    def require(self, run_id):
        if self.lease is None or self.run_id != run_id or self._read()['run_id'] != run_id: raise _fail()

    def register(self, run_id, eval_id):
        self.require(run_id)
        value = self._read()
        if eval_id not in value['expected']:
            value['expected'].append(eval_id)
            _write(self.active_path, value)

    def _finish(self, run_id, status, reason=None):
        self.require(run_id)
        value = self._read()
        if status == 'completed' and value['expected'] and (self.verify is None or not self.verify(run_id, value['expected'])):
            raise _fail()
        value.update({'status':status, 'reason':reason})
        _write(self.root / (run_id + '.json'), value)
        self.active_path.unlink()
        self.close()

    def complete(self, run_id): self._finish(run_id, 'completed')

    def seal(self, run_id, reason):
        if not isinstance(reason, str) or not reason.strip(): raise ValueError('seal reason required')
        self._finish(run_id, 'sealed', reason)

    def close(self):
        if self.lease is not None: self.lease.close()
        self.lease, self.run_id = None, None
