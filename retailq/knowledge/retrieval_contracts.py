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


@dataclass(frozen=True)
class RawHit:
    id: int
    score: float
    fingerprint: str
