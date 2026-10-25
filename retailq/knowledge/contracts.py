from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ChunkDraft:
    category: str
    questions: str
    answer: str
    section_path: str | None
    content_type: str | None
    is_key_clause: bool = False

    def __post_init__(self):
        for name, maximum in [('category',255), ('section_path',512), ('content_type',32)]:
            value = getattr(self,name)
            if value is not None and len(value) > maximum:
                raise ValueError(f'{name} exceeds {maximum} characters')
        for value in [self.category,self.questions,self.answer]:
            if not isinstance(value,str) or not value.strip():
                raise ValueError('knowledge fields must be nonempty strings')
        if any(len(v.encode('utf-8')) > 65535 for v in [self.questions,self.answer]):
            raise ValueError('knowledge TEXT exceeds utf8 byte limit')


@dataclass(frozen=True)
class ChunkRecord:
    id: int
    draft: ChunkDraft
    prev_chunk_id: int | None
    next_chunk_id: int | None
    vector_id: str | None
    vectorize_status: Literal['pending','done']


@dataclass(frozen=True)
class StagedQA:
    id: int
    batch_no: str
    source_ref: str | None
    question: str
    answer: str
    status: Literal['extracted','kept','discarded']


@dataclass(frozen=True)
class QAProposal:
    source_ref: str
    question: str
    answer: str
    evidence: str


@dataclass(frozen=True)
class DedupGroup:
    member_ids: tuple[int,...]
    representative_id: int | None
    target_chunk_id: int | None
    questions: tuple[str,...]

    def __post_init__(self):
        if (self.representative_id is None) == (self.target_chunk_id is None):
            raise ValueError('choose exactly one representative or existing chunk')
        if not self.member_ids or len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError('dedup group members must be unique and nonempty')
