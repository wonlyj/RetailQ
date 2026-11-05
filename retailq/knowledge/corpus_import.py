"""Prepare a recoverable corpus version without publishing it."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import os
import re
import tempfile

from retailq.errors import ServiceError
from retailq.knowledge.contracts import SourceDocument
from retailq.knowledge.corpus import CorpusPlan, CorpusSource, corpus_error
from retailq.knowledge.markdown import chunk_document, load_manifest
from retailq.knowledge.text import content_fingerprint, normalize_text


_KNOWN_SOURCE_DIGESTS = {
    'after-sales-manual.md': 'a813ff4b0e1e219ef08fd7ceaf72052840229a5f00b60f09f19e139261716e98',
    'product-faq.md': 'a1bcd307c3a51d0e45f8bf59223eef7aeb90c778ece4e7cf2edf60a3ce488c8e',
    'shipping-policy.md': '20c0a2431e8f9f90a9d94045f4ac31155175ad4ed0fc8875f2c7ac2a80cfc16b',
}
_OLD_SHIPPING = '演示规则：普通配送订单满99元免运费，不满99元收取8元运费；偏远地区及特殊商品以平台结算页为准。'
_NEW_SHIPPING = _OLD_SHIPPING.replace('收取8元', '收取10元')
_KNOWN_LEGACY = {
    'legacy-faq/1': ('售后', '退货政策是什么', '演示规则：签收后7天内，商品完好且不影响二次销售，可申请退货；具体资格和费用以平台售后规则为准。'),
    'legacy-faq/3': ('发票', '如何开具发票', '演示规则：可在订单详情申请电子发票；具体操作以平台页面为准。'),
}


def _archive(corpus, document: SourceDocument) -> CorpusSource:
    content = normalize_text(document.text).encode('utf-8')
    digest = sha256(content).hexdigest()
    key_hash = sha256(document.key.encode('utf-8')).hexdigest()
    relative = f'sources/{key_hash}/{digest}.md'
    path = corpus._path(*relative.split('/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise corpus_error('已有来源归档与摘要不一致。')
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return CorpusSource(document.key, digest, document.content_type, document.root_category,
                        document.key_sections, relative)


def _load_archive(corpus, source: CorpusSource) -> SourceDocument:
    path = corpus._path(*source.archive.split('/'))
    try:
        data = path.read_bytes()
        if sha256(data).hexdigest() != source.digest:
            raise ValueError('source digest mismatch')
        return SourceDocument(source.key, data.decode('utf-8'), source.content_type,
                              source.root_category, source.key_sections)
    except (OSError, ValueError):
        raise corpus_error('语料来源归档损坏或不存在。') from None


def _section_ranges(text: str):
    lines = text.splitlines()
    titles, sections, current, fence = [], [], None, None
    for number, line in enumerate(lines, 1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        heading = re.match(r'^\s{0,3}(#{1,6})\s+(.+?)\s*$', line) if fence is None else None
        if not heading:
            continue
        if current:
            sections.append((current[0], current[1], number - 1))
        level = len(heading[1])
        titles = [entry for entry in titles if entry[0] < level]
        titles.append((level, re.sub(r'\s+#+\s*$', '', heading[2])))
        current = ('/'.join(title for _, title in titles), number)
    if current:
        sections.append((current[0], current[1], len(lines)))
    return lines, sections


def _locate(document: SourceDocument, draft) -> tuple[int, int]:
    parts = (draft.section_path or '').split(' / ')
    if len(parts) < 3:
        raise corpus_error('知识块缺少来源章节。')
    section_path = ' / '.join(parts[1:-1])
    lines, ranges = _section_ranges(normalize_text(document.text))
    matches = []
    for path, start, end in ranges:
        if path != section_path:
            continue
        section = '\n'.join(lines[start:end])
        fragments = [part.strip() for part in draft.answer.splitlines() if part.strip()]
        if all(fragment in section for fragment in fragments):
            matches.append((start, end))
    if len(matches) != 1:
        raise corpus_error('来源章节定位不唯一或与块内容不一致。')
    return matches[0]


class ReviewApprovalResolver:
    """Resolve only audited approvals whose SQL application is still intact."""
    def __init__(self, store, repository):
        self.store, self.repository = store, repository

    def __call__(self):
        from retailq.knowledge.review_contracts import AuditFault
        approved = set()
        for run in self.store.list_runs():
            if isinstance(run, AuditFault):
                raise corpus_error('审核记录损坏，无法确认保留知识。')
            for group_id in run.group_ids:
                group = self.store.get_group(run.run_id, group_id)
                if not group.decision or group.decision.kind != 'approved':
                    continue
                if not group.application or group.application.phase != 'applied':
                    continue
                if self.repository.inspect_application(group) != 'committed':
                    raise corpus_error('审核记录与数据库回执不一致。')
                approved.add(group.application.prepared.chunk_id)
        return tuple(sorted(approved))


class CorpusImporter:
    def __init__(self, repository, corpus, budget, *, approved_ids=(), approval_resolver=None, collection_prefix='knowledge'):
        self.repository, self.corpus, self.budget = repository, corpus, budget
        self.approved_ids = tuple(approved_ids)
        self.approval_resolver = approval_resolver
        self.collection_prefix = collection_prefix

    def prepare(self, manifest: Path, *, operation_id: str) -> CorpusPlan:
        if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
            raise corpus_error('语料构建标识无效。')
        documents = load_manifest(manifest)
        if not documents or len({d.key for d in documents}) != len(documents):
            raise corpus_error('语料来源为空或重复。')
        plan_path = self.corpus._path('builds', operation_id + '.json')
        if plan_path.exists():
            plan = self.corpus.get_plan(operation_id)
            current = tuple((d.key, sha256(normalize_text(d.text).encode('utf-8')).hexdigest()) for d in documents)
            if current != tuple((s.key, s.digest) for s in plan.sources):
                raise corpus_error('构建标识已绑定其他来源。')
            return self.resume(operation_id)
        sources = tuple(_archive(self.corpus, document) for document in documents)
        try:
            previous = self.corpus.active()
        except ServiceError:
            if self.corpus._path('active.json').exists():
                raise
            previous = None
        plan = CorpusPlan(operation_id, previous.generation if previous else 0,
                          f'{self.collection_prefix}_{operation_id}', '', sources, (), (), (), 'prepared')
        if previous and not {source.key for source in previous.sources} <= {source.key for source in sources}:
            raise corpus_error('新语料缺少之前活动的来源，请核查。')
        confirmed = set(self.approval_resolver() if self.approval_resolver else ())
        if not set(self.approved_ids) <= confirmed:
            raise corpus_error('指定保留知识缺少审核回执。')
        requested_approved = confirmed | (set(previous.approved_ids) if previous else set())
        rows, approved, retired = self._existing(plan, requested_approved)
        retired = tuple(sorted(set(retired) | (set(previous.retired_ids) if previous else set())))
        fingerprints = tuple(sorted((row.id, content_fingerprint(row.draft)) for row in rows if row.id in approved))
        plan = replace(plan, active_ids=approved, approved_ids=approved, retired_ids=retired, fingerprints=fingerprints)
        self.corpus.stage(plan)
        return self.resume(operation_id)

    def _existing(self, plan, approved_ids):
        rows = self.repository.all_chunks()
        by_source = {source.key: source for source in plan.sources}
        previous = None
        if self.corpus._path('active.json').exists():
            previous = self.corpus.active()
        previous_sources = {source.key: source for source in previous.sources} if previous else {}
        previous_fingerprints = dict(previous.fingerprints) if previous else {}
        previous_retired = set(previous.retired_ids) if previous else set()
        confirmed = set(self.approval_resolver() if self.approval_resolver else ())
        approved = set(approved_ids)
        retired = set()
        for row in rows:
            path = row.draft.section_path or ''
            if path == 'legacy-faq/2':
                if row.draft.answer in (_OLD_SHIPPING, _NEW_SHIPPING) and row.draft.questions == '运费标准是什么' and row.draft.category == '配送' and row.draft.content_type == 'faq':
                    retired.add(row.id)
                else:
                    raise corpus_error('已有运费知识与旧种子不一致，请核查。')
            elif path.startswith('legacy-faq/'):
                known = _KNOWN_LEGACY.get(path)
                if known and (row.draft.category, row.draft.questions, row.draft.answer) == known and row.draft.content_type == 'faq':
                    approved.add(row.id)
                elif row.id not in approved:
                    raise corpus_error('旧FAQ块缺少已确认的保留依据。')
            elif '@' in path and ' / ' in path:
                source_ref = path.split(' / ', 1)[0]
                key, digest = source_ref.rsplit('@', 1)
                source = by_source.get(key)
                if source and digest == source.digest:
                    continue
                if source and digest == _KNOWN_SOURCE_DIGESTS.get(key):
                    retired.add(row.id)
                elif row.id in previous_retired:
                    retired.add(row.id)
                elif (source and (old := previous_sources.get(key)) and old.digest == digest
                      and row.id in old.chunk_ids
                      and previous_fingerprints.get(row.id) == content_fingerprint(row.draft)):
                    retired.add(row.id)
                elif row.id not in approved:
                    raise corpus_error('存在未知来源版本，请先核查。')
            elif row.id not in approved:
                raise corpus_error('知识块缺少已确认的来源或审核依据。')
        known_ids = {row.id for row in rows if (row.draft.section_path in _KNOWN_LEGACY and
                     (row.draft.category, row.draft.questions, row.draft.answer) == _KNOWN_LEGACY[row.draft.section_path] and
                     row.draft.content_type == 'faq')}
        if not approved <= confirmed | known_ids or not approved <= {row.id for row in rows} or approved & retired:
            raise corpus_error('批准知识清单与数据库不一致。')
        return rows, tuple(sorted(approved)), tuple(sorted(retired))

    def resume(self, operation_id: str) -> CorpusPlan:
        plan = self.corpus.get_plan(operation_id)
        prepared = []
        for source in plan.sources:
            document = _load_archive(self.corpus, source)
            drafts = chunk_document(document, self.budget)
            ranges = tuple(_locate(document, draft) for draft in drafts)
            prepared.append((source, drafts, ranges))
        if plan.phase != 'prepared':
            rows = self.repository.get_chunks(list(plan.active_ids))
            if set(rows) != set(plan.active_ids) or any(content_fingerprint(rows[i].draft) != fp for i, fp in plan.fingerprints):
                raise corpus_error('数据库知识块与构建快照不一致。')
            for source, drafts, ranges in prepared:
                if len(source.chunk_ids) != len(drafts) or source.line_ranges != tuple((i, *r) for i, r in zip(source.chunk_ids, ranges)):
                    raise corpus_error('数据库知识块与来源定位不一致。')
                if any(rows[i].draft != draft for i, draft in zip(source.chunk_ids, drafts)):
                    raise corpus_error('数据库知识块与来源定位不一致。')
                for index, item_id in enumerate(source.chunk_ids):
                    previous = source.chunk_ids[index - 1] if index else None
                    following = source.chunk_ids[index + 1] if index + 1 < len(source.chunk_ids) else None
                    if rows[item_id].prev_chunk_id != previous or rows[item_id].next_chunk_id != following:
                        raise corpus_error('数据库知识块链接不一致。')
            self._existing(plan, plan.approved_ids)
            return plan
        rows, approved, retired = self._existing(plan, plan.approved_ids)
        retired = tuple(sorted(set(retired) | set(plan.retired_ids)))
        self.repository.migrate_known_shipping_faq()
        sources = []
        active_ids = list(approved)
        fingerprints = {row.id: content_fingerprint(row.draft) for row in rows if row.id in approved}
        for source, drafts, ranges in prepared:
            ids = tuple(self.repository.import_chunks(drafts))
            locators = tuple((item_id, *location) for item_id, location in zip(ids, ranges))
            sources.append(replace(source, chunk_ids=ids, line_ranges=locators))
            active_ids.extend(ids)
            fingerprints.update((item_id, content_fingerprint(draft)) for item_id, draft in zip(ids, drafts))
        completed = replace(plan, sources=tuple(sources), active_ids=tuple(active_ids), retired_ids=retired,
                            fingerprints=tuple(sorted(fingerprints.items())), approved_ids=approved, phase='sql_committed')
        self.corpus.update(completed, expected_phase='prepared')
        return completed
