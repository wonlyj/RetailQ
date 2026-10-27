"""Immutable, validated audit contracts for the local review workflow."""
from datetime import datetime
import hashlib
import json
import re
import unicodedata
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from retailq.knowledge.contracts import ChunkRecord, ConversationSnapshot

DBID = Annotated[int, Field(ge=1, le=2**63 - 1)]
Revision = Annotated[int, Field(ge=0, le=2**53 - 1)]
Token = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Nonnegative = Annotated[int, Field(ge=0)]
T = TypeVar('T')


def parse_db_id(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise ValueError('expected canonical decimal ID')
    result = int(value)
    if result > 2**63 - 1:
        raise ValueError('ID exceeds INT64')
    return result


def normalize_category(value: str) -> str:
    if not isinstance(value, str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('category contains a control or format character')
    value = unicodedata.normalize('NFC', value).strip()
    if not 1 <= len(value) <= 255:
        raise ValueError('category must contain 1–255 characters')
    return value


class FrozenRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True, validate_default=True)


def evolve(record: T, **changes) -> T:
    """Revalidate updates; model_copy(update=...) intentionally bypasses validation."""
    fields = {name: getattr(record, name) for name in type(record).model_fields}
    return type(record).model_validate({**fields, **changes})


class Page(FrozenRecord, Generic[T]):
    items: tuple[T, ...]
    total: Nonnegative
    offset: Nonnegative
    limit: Annotated[int, Field(ge=1, le=100)]


class SafeFailure(FrozenRecord):
    code: Annotated[str, Field(min_length=1, max_length=100)]
    message: Annotated[str, Field(min_length=1, max_length=512)]


class AuditFault(SafeFailure):
    run_id: Token


class SourceQA(FrozenRecord):
    staging_id: DBID
    batch_no: Annotated[str, Field(min_length=1, max_length=64)]
    source_ref: Annotated[str, Field(min_length=1, max_length=255)]
    question: Annotated[str, Field(min_length=1)]
    answer: Annotated[str, Field(min_length=1)]
    message_ids: Annotated[tuple[DBID, ...], Field(min_length=1)]


class SourceSummary(FrozenRecord):
    id: DBID
    status: str
    eligibility: bool
    reason: str
    message_count: Nonnegative
    max_message_id: DBID | None


class CategorySuggestion(FrozenRecord):
    group_id: Token
    category: str | None
    reason: Annotated[str, Field(max_length=300)]
    recommend_reject: bool = False

    @field_validator('category')
    @classmethod
    def category_valid(cls, value):
        return normalize_category(value) if value is not None else None


class CategoryCandidate(FrozenRecord):
    group_id: Token
    questions: tuple[str, ...]
    answer: str
    categories: tuple[str, ...]
    catalog_truncated: bool


class Timestamped(FrozenRecord):
    @field_validator('created_at', 'updated_at', 'accepted_at', 'decided_at', check_fields=False)
    @classmethod
    def shanghai_time(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 28800:
            raise ValueError('timestamp must include +08:00 offset')
        return value


class ReviewDecision(Timestamped):
    operation_id: Token
    kind: Literal['approved', 'rejected']
    category: str | None = None
    reason: Annotated[str, Field(max_length=512)] | None = None
    decided_at: str
    actor: Literal['本机操作'] = '本机操作'

    @field_validator('category')
    @classmethod
    def category_valid(cls, value):
        return normalize_category(value) if value is not None else None


class MemberDisposition(FrozenRecord):
    staging_id: DBID
    status: Literal['kept', 'discarded']
    reason: Literal['new', 'same_group', 'merge_existing', 'human_rejected']
    chunk_id: DBID | None


class PreparedApplication(FrozenRecord):
    operation_id: Token
    chunk_id: DBID | None
    fingerprint: Digest | None
    members: Annotated[tuple[MemberDisposition, ...], Field(min_length=1)]


class ApplicationRecord(FrozenRecord):
    phase: Literal['prepared', 'applied', 'conflict']
    prepared: PreparedApplication
    error: SafeFailure | None = None
    vector_requested_ids: tuple[DBID, ...] = ()
    vector_confirmed_ids: tuple[DBID, ...] = ()


class ReviewGroup(FrozenRecord):
    run_id: Token
    group_id: Token
    revision: Revision = 0
    members: Annotated[tuple[SourceQA, ...], Field(min_length=1)]
    representative_id: DBID | None = None
    target_chunk_id: DBID | None = None
    target_fingerprint: Digest | None = None
    questions: Annotated[tuple[str, ...], Field(min_length=1)]
    answer: Annotated[str, Field(min_length=1)]
    suggestion: CategorySuggestion | None = None
    decision: ReviewDecision | None = None
    application: ApplicationRecord | None = None

    @model_validator(mode='after')
    def valid_group(self):
        ids = tuple(m.staging_id for m in self.members)
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate group member')
        if (self.representative_id is None) == (self.target_chunk_id is None):
            raise ValueError('choose exactly one representative or existing target')
        if self.representative_id is not None and self.representative_id not in ids:
            raise ValueError('representative must be a member')
        if self.target_chunk_id is not None and self.target_fingerprint is None:
            raise ValueError('existing target requires a fingerprint')
        if self.suggestion and self.suggestion.group_id != self.group_id:
            raise ValueError('suggestion belongs to another group')
        if set(self.questions) != {m.question for m in self.members}:
            raise ValueError('questions must preserve every member question')
        if self.application and (not self.decision or
                                self.application.prepared.operation_id != self.decision.operation_id):
            raise ValueError('application requires the same durable decision')
        return self


class BatchRecord(FrozenRecord):
    batch_no: Digest
    sources: tuple[ConversationSnapshot, ...]


class RunCounters(FrozenRecord):
    extracted: Nonnegative = 0
    review_pending: Nonnegative = 0
    approved: Nonnegative = 0
    rejected: Nonnegative = 0
    applied: Nonnegative = 0
    vectorized: Nonnegative = 0
    pending_vectors: Nonnegative = 0


class SelectionSummary(FrozenRecord):
    eligible_total: Nonnegative
    selected: Nonnegative
    remaining: Nonnegative


RunPhase = Literal['queued', 'extraction', 'dedup', 'classification', 'waiting_review',
                   'applying', 'vectorize', 'completed', 'failed', 'interrupted', 'conflict', 'sealed']
OperationPhase = Literal['queued', 'running', 'succeeded', 'failed', 'interrupted', 'sealed']


class RunRecord(Timestamped):
    schema_version: Literal[2] = 2
    run_id: Token
    revision: Revision = 0
    rule_hash: Digest
    sources: tuple[ConversationSnapshot, ...] = ()
    batches: tuple[BatchRecord, ...] = ()
    completed_batches: tuple[Digest, ...] = ()
    group_ids: tuple[Token, ...] = ()
    # Frozen dedup intent precedes individual group files, so partial file writes
    # can resume without asking a nondeterministic model to regroup the sources.
    group_plan: tuple[ReviewGroup, ...] | None = None
    phase: RunPhase = 'queued'
    counters: RunCounters = Field(default_factory=RunCounters)
    error: SafeFailure | None = None
    created_at: str
    updated_at: str


class PrepareCommand(FrozenRecord):
    kind: Literal['prepare'] = 'prepare'
    request_key: Token
    conversation_ids: Annotated[tuple[DBID, ...], Field(min_length=1, max_length=100)] | None = None

    @field_validator('conversation_ids')
    @classmethod
    def unique_ids(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('duplicate conversation ID')
        return tuple(sorted(value)) if value is not None else None


class ResumeCommand(FrozenRecord):
    kind: Literal['resume'] = 'resume'
    request_key: Token
    run_id: Token


class DecisionCommand(FrozenRecord):
    kind: Literal['decide'] = 'decide'
    request_key: Token
    run_id: Token
    group_id: Token
    expected_revision: Revision
    decision: Literal['approve', 'reject']
    category: str | None = None
    reason: Annotated[str, Field(max_length=512)] | None = None


class RetryVectorsCommand(FrozenRecord):
    kind: Literal['retry_vectors'] = 'retry_vectors'
    request_key: Token
    run_id: Token


class SealCommand(FrozenRecord):
    kind: Literal['seal'] = 'seal'
    request_key: Token
    run_id: Token
    confirm_run_id: Token
    reason: Annotated[str, Field(max_length=512)] | None = None

    @model_validator(mode='after')
    def confirmed(self):
        if self.confirm_run_id != self.run_id:
            raise ValueError('confirm the same run')
        return self


ConsoleCommand = Annotated[PrepareCommand | ResumeCommand | DecisionCommand | RetryVectorsCommand | SealCommand,
                           Field(discriminator='kind')]


def canonical_request_hash(command: ConsoleCommand) -> str:
    value = command.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


class OperationRecord(Timestamped):
    operation_id: Token
    run_id: Token
    kind: Literal['prepare', 'resume', 'decide', 'retry_vectors', 'seal']
    request_hash: Digest
    command: ConsoleCommand
    source_snapshot: tuple[ConversationSnapshot, ...] = ()
    selection: SelectionSummary | None = None
    rule_hash: Digest | None = None
    phase: OperationPhase = 'queued'
    revision: Revision = 0
    accepted_at: str
    error: SafeFailure | None = None
    reused_operation_id: Token | None = None

    @model_validator(mode='after')
    def correct_command(self):
        if self.operation_id != self.command.request_key or self.kind != self.command.kind:
            raise ValueError('operation identity does not match command')
        if self.request_hash != canonical_request_hash(self.command):
            raise ValueError('command hash mismatch')
        expected_run = self.operation_id if self.kind == 'prepare' else self.command.run_id
        if self.run_id != expected_run:
            raise ValueError('operation run mismatch')
        if self.reused_operation_id == self.operation_id:
            raise ValueError('operation cannot alias itself')
        return self


class SealRecord(Timestamped):
    run_id: Token
    operation_id: Token
    created_at: str
    reason: Annotated[str, Field(max_length=512)]
    actor: Literal['本机操作'] = '本机操作'


class ConsoleSummary(FrozenRecord):
    chunks: Nonnegative
    counters: RunCounters | None
    current_run_id: Token | None
    phase: RunPhase | None
    vector_verification: Literal['not_checked'] = 'not_checked'
    error: SafeFailure | None = None


class RunSummary(FrozenRecord):
    run_id: Token
    kind: Literal['review', 'legacy']
    phase: str
    counters: RunCounters | None
    created_at: str | None
    error: SafeFailure | None = None


class RunDetail(FrozenRecord):
    summary: RunSummary
    sources: tuple[ConversationSnapshot, ...] | None
    groups_count: Nonnegative | None
    completed_batches: Nonnegative | None = None
    total_batches: Nonnegative | None = None


class GroupSummary(FrozenRecord):
    group_id: Token
    revision: Revision
    question: str
    disposition: Literal['new', 'merge', 'rejected', 'pending']
    decision: Literal['pending', 'approved', 'rejected']
    category: str | None
    suggestion: CategorySuggestion | None


class GroupDetail(FrozenRecord):
    summary: GroupSummary
    members: tuple[SourceQA, ...]
    dispositions: tuple[MemberDisposition, ...] = ()
    questions: tuple[str, ...]
    answer: str
    target_confirmed: bool
    planned_target_id: DBID | None
    target_chunk_id: DBID | None
    application_phase: Literal['prepared', 'applied', 'conflict'] | None
    vectorize_status: Literal['pending', 'done'] | None
    error: SafeFailure | None = None


class ChunkDetail(FrozenRecord):
    record: ChunkRecord


class OperationView(FrozenRecord):
    operation_id: Token
    run_id: Token
    kind: Literal['prepare', 'resume', 'decide', 'retry_vectors', 'seal']
    phase: OperationPhase
    error: SafeFailure | None = None


class LegacyTarget(FrozenRecord):
    group_index: Nonnegative
    chunk_id: DBID
    disposition: Literal['legacy_explicit'] = 'legacy_explicit'


class LegacyRunDetail(FrozenRecord):
    run_id: Token
    phase: str
    sources: tuple[ConversationSnapshot, ...]
    explicit_targets: tuple[LegacyTarget, ...]
    missing_provenance: Nonnegative
