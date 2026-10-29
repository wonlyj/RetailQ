"""Shared value objects for indexed knowledge and retrieval."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexedChunk:
    id: int
    vector: list[float]
    fingerprint: str
    text: str
    category: str
    content_type: str
    source_key: str
    source_version: str


@dataclass(frozen=True)
class RetrievalFilters:
    category: str | None = None
    content_type: str | None = None
    category_prefix: str | None = None


@dataclass(frozen=True)
class RawHit:
    id: int
    score: float
    fingerprint: str


@dataclass(frozen=True)
class QueryUnderstanding:
    raw_question: str
    normalized_query: str
    bm25_query: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class EvidenceChunk:
    id: int
    draft: object
    rank: int
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    understanding: QueryUnderstanding
    snapshot: object
    strategy: str
    candidates: tuple[EvidenceChunk, ...]
    candidate_hits: tuple[RawHit, ...]
