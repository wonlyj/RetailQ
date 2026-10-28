"""Atomic local review audit. Callers own the runtime maintenance lease."""
from datetime import datetime
from pathlib import Path
import hashlib
import os
import re
import tempfile
from threading import RLock
from typing import Callable

from pydantic import ValidationError

from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import (
    AuditFault, OperationRecord, ReviewGroup, RunRecord, SealRecord, evolve,
)


def review_error(code='knowledge_review_conflict', message='审核记录不一致，请核查或终止并封存。', status=409):
    return ServiceError(code, status, message)


class ReviewStore:
    def __init__(self, root: Path, *, clock: Callable[[], datetime]):
        self.root = Path(root).resolve()
        self.clock = clock
        self._lock = RLock()

    def timestamp(self):
        from datetime import timezone, timedelta
        return self.clock().astimezone(timezone(timedelta(hours=8))).isoformat()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
            raise review_error('knowledge_review_invalid_id', '审核ID格式无效。', 422)
        return value

    def _path(self, *parts):
        base = self.root / 'reviews'
        path = base.joinpath(*parts)
        # Both root and reviews containment matter when a junction already exists.
        resolved_base = base.resolve()
        resolved = path.resolve()
        if not resolved_base.is_relative_to(self.root) or not resolved.is_relative_to(resolved_base):
            raise review_error('knowledge_review_corrupt', '审核目录存在不安全链接。')
        return path

    def _read(self, path, model):
        try:
            return model.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            raise review_error('knowledge_review_not_found', '审核记录不存在。', 404) from None
        except (OSError, ValueError, ValidationError):
            raise review_error('knowledge_review_corrupt', '审核记录损坏或无法读取，请核查。') from None

    def _write(self, path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after creating parents; never follow a preexisting escape.
        self._path(*path.relative_to(self.root / 'reviews').parts)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.audit-', delete=False) as f:
                temporary = Path(f.name)
                f.write(record.model_dump_json(indent=2).encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _put(self, path, record, expected_revision, immutable=()):
        # Revalidate even if a caller obtained an instance by unsafe model_construct.
        record = type(record).model_validate_json(record.model_dump_json())
        if path.exists():
            old = self._read(path, type(record))
            if expected_revision is None:
                if isinstance(record, OperationRecord):
                    if old.request_hash == record.request_hash:
                        return old
                    raise review_error('knowledge_request_conflict', '该请求键已用于其他操作。')
                raise review_error('knowledge_review_stale', '审核记录已存在，请刷新。')
            if old.revision != expected_revision or record.revision != expected_revision:
                raise review_error('knowledge_review_stale', '审核版本已变化，请刷新。')
            if any(getattr(old, key) != getattr(record, key) for key in immutable):
                raise review_error(message='不能替换已冻结的审核来源或命令。')
            record = evolve(record, revision=expected_revision + 1)
        elif expected_revision is not None or record.revision != 0:
            raise review_error('knowledge_review_stale', '审核记录版本无效，请刷新。')
        self._write(path, record)
        return record

    def get_run(self, run_id):
        with self._lock:
            run = self._read(self._path('runs', self._id(run_id), 'run.json'), RunRecord)
            if run.run_id != run_id:
                raise review_error('knowledge_review_corrupt','运行记录与文件身份不一致。')
            return evolve(run, phase='sealed') if self.get_seal(run_id) else run

    def put_run(self, record, *, expected_revision):
        with self._lock:
            if self.get_seal(record.run_id):
                raise review_error('knowledge_review_sealed', '任务已终止并封存。')
            path = self._path('runs', self._id(record.run_id), 'run.json')
            if expected_revision is None and not path.exists():
                pending = self.unfinished()
                if pending is not None and pending.run_id != record.run_id:
                    raise review_error('knowledge_review_pending', '已有未结束审核任务，请先处理。')
            return self._put(path, record, expected_revision, ('run_id', 'rule_hash', 'sources', 'created_at'))

    def get_group(self, run_id, group_id):
        group=self._read(self._path('runs', self._id(run_id), 'groups', self._id(group_id) + '.json'), ReviewGroup)
        if group.run_id != run_id or group.group_id != group_id:
            raise review_error('knowledge_review_corrupt','审核组与文件身份不一致。')
        return group

    def put_group(self, record, *, expected_revision):
        with self._lock:
            if self.get_seal(record.run_id):
                raise review_error('knowledge_review_sealed', '任务已终止并封存。')
            return self._put(self._path('runs', self._id(record.run_id), 'groups',
                                        self._id(record.group_id) + '.json'), record, expected_revision,
                             ('run_id', 'group_id', 'members', 'representative_id', 'target_chunk_id',
                              'target_fingerprint', 'questions', 'answer'))

    def get_operation(self, operation_id):
        operation=self._read(self._path('operations', self._id(operation_id) + '.json'), OperationRecord)
        if operation.operation_id != operation_id:
            raise review_error('knowledge_review_corrupt','操作记录与文件身份不一致。')
        return operation

    def put_operation(self, record, *, expected_revision):
        with self._lock:
            return self._put(self._path('operations', self._id(record.operation_id) + '.json'), record,
                             expected_revision, ('operation_id', 'run_id', 'kind', 'request_hash', 'command',
                                                 'source_snapshot', 'selection', 'rule_hash', 'accepted_at',
                                                 'reused_operation_id'))

    def list_operations(self):
        """Executable validated commands only; list_runs surfaces every fault."""
        with self._lock:
            return self._scan_operations()[0]

    def _scan_operations(self):
        valid,bad=[],[]
        for path in sorted(self._path('operations').glob('*.json')):
            try:
                valid.append(self.get_operation(self._id(path.stem)))
            except ServiceError:
                # Invalid filenames also occupy a stable diagnostic slot. Never
                # parse broken JSON to guess a command, run or approval.
                identity=(path.stem if re.fullmatch(r'[0-9a-f]{32}',path.stem) else
                          hashlib.sha256(('operation-file:'+path.name).encode()).hexdigest()[:32])
                bad.append(identity)
        return tuple(valid),tuple(bad)

    def operation_faults(self):
        with self._lock:
            return self._operation_faults(*self._scan_operations())

    def _operation_faults(self, valid, bad):
        if not bad:
            return ()
        bindings={identity:set() for identity in bad}
        def bind(operation_id,run_id):
            if operation_id in bindings:
                bindings[operation_id].add(run_id)
        for operation in valid:
            if operation.reused_operation_id:
                bind(operation.reused_operation_id,operation.run_id)
        root=self._path('runs')
        for directory in sorted(root.iterdir()) if root.exists() else ():
            if not re.fullmatch(r'[0-9a-f]{32}',directory.name):
                continue
            rid=directory.name
            try:
                run=self.get_run(rid)
                bind(run.run_id,run.run_id)  # A prepare's key equals its run ID.
            except ServiceError:
                pass
            try:
                marker=self.get_seal(rid)
                if marker:
                    bind(marker.operation_id,rid)
            except ServiceError:
                pass
            for path in self._path('runs',rid,'groups').glob('*.json'):
                try:
                    group=self.get_group(rid,self._id(path.stem))
                    if group.decision:
                        bind(group.decision.operation_id,rid)
                except ServiceError:
                    pass
        faults=[]
        for identity in bad:
            candidates=bindings[identity]
            if len(candidates)==1:
                rid=next(iter(candidates))
                message=f'操作审计 {identity} 损坏；由有效记录关联到任务 {rid}，请核查或终止并封存。'
            else:
                rid=identity
                message=f'操作审计 {identity} 损坏，归属无法确证；此ID仅表示独立故障记录。封存只确认本故障，其他未结束任务仍需分别处理。'
            faults.append(AuditFault(run_id=rid,code='knowledge_operation_corrupt',message=message))
        return tuple(faults)

    def list_runs(self):
        with self._lock:
            root = self._path('runs')
            results = []
            for directory in sorted(root.iterdir()) if root.exists() else ():
                if directory.name.startswith('.'):
                    continue
                run_id = self._id(directory.name)
                try:
                    run = self.get_run(run_id)
                    if run.phase != 'sealed':
                        for gid in run.group_ids:
                            self.get_group(run_id, gid)
                    results.append(run)
                except ServiceError:
                    results.append(AuditFault(run_id=run_id, code='knowledge_review_corrupt',
                                               message='审核记录损坏或不完整，请核查或终止并封存。'))
            known = {r.run_id for r in results}
            valid,bad=self._scan_operations()
            for operation in valid:
                if operation.kind == 'prepare' and operation.run_id not in known:
                    if operation.rule_hash is None:
                        results.append(AuditFault(run_id=operation.run_id, code='knowledge_review_corrupt',
                                                   message='准备命令缺少规则快照。'))
                    else:
                        results.append(RunRecord(run_id=operation.run_id, rule_hash=operation.rule_hash,
                                                 sources=operation.source_snapshot, phase='interrupted',
                                                 created_at=operation.accepted_at, updated_at=operation.accepted_at))
            # Faults replace untrustworthy task summaries but do not hide other
            # valid tasks. A seal remains independently readable on restart.
            by_id={r.run_id:r for r in results}
            for fault in self._operation_faults(valid,bad):
                by_id[fault.run_id]=fault
            results=list(by_id.values())
            return tuple(results)

    def unfinished(self):
        with self._lock:
            for run in self.list_runs():
                if self.get_seal(run.run_id):
                    continue
                if isinstance(run, AuditFault) or run.phase not in ('completed', 'sealed'):
                    return run
            return None

    def get_seal(self, run_id):
        path = self._path('runs', self._id(run_id), 'sealed.json')
        marker=self._read(path, SealRecord) if path.exists() else None
        if marker and marker.run_id != run_id:
            raise review_error('knowledge_review_corrupt','封存标记与文件身份不一致。')
        return marker

    def seal(self, run_id, operation_id, reason):
        with self._lock:
            old = self.get_seal(run_id)
            if old:
                return old
            marker = SealRecord(run_id=self._id(run_id), operation_id=self._id(operation_id),
                                created_at=self.timestamp(), reason=reason)
            self._write(self._path('runs', run_id, 'sealed.json'), marker)
            return marker
