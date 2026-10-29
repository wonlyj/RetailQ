"""Atomic, versioned inventory of the chunks eligible for retrieval."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from retailq.errors import ServiceError
from retailq.knowledge.text import content_fingerprint


def corpus_error(message='知识语料清单不可用，请先核查或完成构建。'):
    return ServiceError('knowledge_corpus_invalid', 409, message)


@dataclass(frozen=True)
class CorpusSource:
    key: str
    digest: str
    content_type: str
    root_category: str
    key_sections: tuple[str, ...]
    archive: str
    chunk_ids: tuple[int, ...] = ()
    line_ranges: tuple[tuple[int, int, int], ...] = ()


@dataclass(frozen=True)
class CorpusPlan:
    build_id: str
    previous_generation: int
    collection: str
    identity_hash: str
    sources: tuple[CorpusSource, ...]
    active_ids: tuple[int, ...]
    retired_ids: tuple[int, ...]
    fingerprints: tuple[tuple[int, str], ...]
    phase: str
    approved_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CorpusSnapshot:
    generation: int
    collection: str
    identity_hash: str
    sources: tuple[CorpusSource, ...]
    active_ids: tuple[int, ...]
    retired_ids: tuple[int, ...]
    fingerprints: tuple[tuple[int, str], ...]
    corpus_hash: str
    approved_ids: tuple[int, ...]
    build_id: str


def _validate(plan: CorpusPlan) -> None:
    if not re.fullmatch(r'[0-9a-f]{32}', plan.build_id):
        raise corpus_error('语料构建标识无效。')
    if plan.previous_generation < 0 or plan.phase not in ('prepared', 'sql_committed', 'indexing', 'verified', 'published'):
        raise corpus_error('语料构建状态无效。')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,254}', plan.collection):
        raise corpus_error('语料集合名称无效。')
    if plan.identity_hash and not re.fullmatch(r'[0-9a-f]{64}', plan.identity_hash):
        raise corpus_error('语料模型身份无效。')
    ids = plan.active_ids
    if (len(ids) != len(set(ids)) or any(type(i) is not int or not 1 <= i < 2**63 for i in ids)
            or set(ids) & set(plan.retired_ids) or len(set(plan.retired_ids)) != len(plan.retired_ids)
            or any(type(i) is not int or not 1 <= i < 2**63 for i in plan.retired_ids)):
        raise corpus_error('语料块清单不一致。')
    if len(plan.approved_ids) != len(set(plan.approved_ids)) or not set(plan.approved_ids) <= set(ids):
        raise corpus_error('批准知识清单不一致。')
    fingerprints = dict(plan.fingerprints)
    if (len(fingerprints) != len(plan.fingerprints) or set(fingerprints) != set(ids)
            or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in fingerprints.values())):
        raise corpus_error('语料指纹清单不一致。')
    if len({source.key for source in plan.sources}) != len(plan.sources):
        raise corpus_error('语料来源重复。')
    source_ids = []
    for source in plan.sources:
        expected_archive = f'sources/{sha256(source.key.encode("utf-8")).hexdigest()}/{source.digest}.md'
        if (not source.key or '\\' in source.key or source.key.startswith('/')
                or any(part in ('', '.', '..') for part in source.key.split('/'))
                or not re.fullmatch(r'[0-9a-f]{64}', source.digest) or source.archive != expected_archive
                or source.content_type not in ('faq', 'policy', 'manual')
                or not set(source.chunk_ids) <= set(ids)):
            raise corpus_error('语料来源不符合清单。')
        source_ids.extend(source.chunk_ids)
        if (len(source.chunk_ids) != len(set(source.chunk_ids))
                or {row[0] for row in source.line_ranges} != set(source.chunk_ids)
                or len(source.line_ranges) != len(source.chunk_ids)
                or any(len(row) != 3 or any(type(x) is not int for x in row)
                       or row[1] < 1 or row[2] < row[1] for row in source.line_ranges)):
            raise corpus_error('语料来源行范围不一致。')
    if (len(source_ids) != len(set(source_ids)) or set(source_ids) & set(plan.approved_ids)
            or set(source_ids) | set(plan.approved_ids) != set(ids)):
        raise corpus_error('语料活动块缺少唯一来源或审核依据。')


def _decode_source(value: dict) -> CorpusSource:
    return CorpusSource(key=value['key'], digest=value['digest'], content_type=value['content_type'],
                        root_category=value['root_category'], key_sections=tuple(value['key_sections']),
                        archive=value['archive'], chunk_ids=tuple(value['chunk_ids']),
                        line_ranges=tuple(tuple(x) for x in value['line_ranges']))


def _decode_plan(value: dict) -> CorpusPlan:
    if set(value) != {'build_id', 'previous_generation', 'collection', 'identity_hash', 'sources',
                      'active_ids', 'retired_ids', 'fingerprints', 'phase', 'approved_ids'}:
        raise ValueError('invalid corpus record fields')
    plan = CorpusPlan(build_id=value['build_id'], previous_generation=value['previous_generation'],
                      collection=value['collection'], identity_hash=value['identity_hash'],
                      sources=tuple(_decode_source(s) for s in value['sources']),
                      active_ids=tuple(value['active_ids']), retired_ids=tuple(value['retired_ids']),
                      fingerprints=tuple(tuple(x) for x in value['fingerprints']), phase=value['phase'],
                      approved_ids=tuple(value['approved_ids']))
    _validate(plan)
    return plan


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CorpusStore:
    def __init__(self, root: Path, coordinator):
        self.root = Path(root).resolve()
        self.coordinator = coordinator
        self.directory = self.root / 'corpus'

    def _path(self, *parts: str) -> Path:
        result = self.directory.joinpath(*parts)
        if not result.resolve().is_relative_to(self.root) or not result.resolve().is_relative_to(self.directory.resolve()):
            raise corpus_error('语料路径超出允许范围。')
        return result

    def get_plan(self, build_id: str) -> CorpusPlan:
        if not re.fullmatch(r'[0-9a-f]{32}', build_id):
            raise corpus_error('语料构建标识无效。')
        try:
            plan = _decode_plan(json.loads(self._path('builds', build_id + '.json').read_text(encoding='utf-8')))
            if plan.build_id != build_id:
                raise ValueError('build identity mismatch')
            return plan
        except (OSError, ValueError, TypeError, KeyError):
            raise corpus_error('语料构建记录损坏或不存在。') from None

    def stage(self, plan: CorpusPlan) -> str:
        _validate(plan)
        if plan.phase != 'prepared':
            raise corpus_error('新语料构建必须从准备阶段开始。')
        with self.coordinator.short_lock('audit'):
            path = self._path('builds', plan.build_id + '.json')
            if path.exists():
                existing = self.get_plan(plan.build_id)
                if existing != plan:
                    raise corpus_error('语料构建标识已用于不同内容。')
            else:
                _write(path, asdict(plan))
        return plan.build_id

    def update(self, plan: CorpusPlan, *, expected_phase: str) -> None:
        _validate(plan)
        with self.coordinator.short_lock('audit'):
            current = self.get_plan(plan.build_id)
            if current.phase != expected_phase:
                raise corpus_error('语料构建阶段已变化。')
            transitions = {'prepared': 'sql_committed', 'sql_committed': 'indexing', 'indexing': 'verified'}
            if transitions.get(current.phase) != plan.phase:
                raise corpus_error('语料构建阶段不能跳跃。')
            if (plan.previous_generation != current.previous_generation or plan.collection != current.collection
                    or (current.identity_hash and plan.identity_hash != current.identity_hash)):
                raise corpus_error('语料构建身份不能替换。')
            def fixed_sources(sources):
                return tuple((s.key, s.digest, s.content_type, s.root_category, s.key_sections, s.archive) for s in sources)
            if fixed_sources(plan.sources) != fixed_sources(current.sources):
                raise corpus_error('语料构建来源不能替换。')
            if current.phase != 'prepared' and (plan.active_ids != current.active_ids or plan.retired_ids != current.retired_ids
                                                or plan.fingerprints != current.fingerprints or plan.approved_ids != current.approved_ids
                                                or plan.sources != current.sources):
                raise corpus_error('已导入的语料清单不能替换。')
            _write(self._path('builds', plan.build_id + '.json'), asdict(plan))

    def active(self) -> CorpusSnapshot:
        try:
            value = json.loads(self._path('active.json').read_text(encoding='utf-8'))
            plan = _decode_plan(value['plan'])
            content = {'generation': value['generation'], 'plan': value['plan']}
            digest = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            if value['corpus_hash'] != digest or plan.phase != 'published' or value['generation'] != plan.previous_generation + 1:
                raise ValueError('invalid active checksum')
            return CorpusSnapshot(value['generation'], plan.collection, plan.identity_hash, plan.sources,
                                  plan.active_ids, plan.retired_ids, plan.fingerprints, digest, plan.approved_ids, plan.build_id)
        except (OSError, ValueError, TypeError, KeyError):
            raise corpus_error() from None

    def publish(self, build_id: str, *, expected_generation: int) -> CorpusSnapshot:
        from dataclasses import replace
        with self.coordinator.short_lock('audit'):
            plan = self.get_plan(build_id)
            try:
                current_snapshot = self.active()
                generation = current_snapshot.generation
            except ServiceError:
                if self._path('active.json').exists():
                    raise
                current_snapshot = None
                generation = 0
            published = replace(plan, phase='published')
            value = asdict(published)
            if (current_snapshot is not None and current_snapshot.build_id == build_id and plan.phase == 'verified'
                    and generation == expected_generation + 1 and plan.previous_generation == expected_generation):
                content = {'generation': generation, 'plan': value}
                digest = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
                if digest != current_snapshot.corpus_hash:
                    raise corpus_error('已发布语料与恢复记录不一致。')
                _write(self._path('builds', build_id + '.json'), value)
                return current_snapshot
            if generation != expected_generation or plan.previous_generation != generation or plan.phase != 'verified':
                raise corpus_error('语料版本或构建阶段不匹配。')
            content = {'generation': generation + 1, 'plan': value}
            digest = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            _write(self._path('active.json'), {'generation': generation + 1, 'corpus_hash': digest, 'plan': value})
            _write(self._path('builds', build_id + '.json'), value)
            return self.active()


def is_active(record, snapshot: CorpusSnapshot) -> bool:
    if (record.id not in snapshot.active_ids or record.id in snapshot.retired_ids
            or dict(snapshot.fingerprints).get(record.id) != content_fingerprint(record.draft)):
        return False
    if record.id in snapshot.approved_ids:
        return True
    return any(record.id in source.chunk_ids and (record.draft.section_path or '').startswith(source.key + '@' + source.digest + ' / ')
               for source in snapshot.sources)
