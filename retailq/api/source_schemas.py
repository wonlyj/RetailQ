from pydantic import BaseModel, ConfigDict, Field


class CitationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    number: int = Field(ge=1, le=10)
    chunk_id: int = Field(ge=1, lt=2**63)
    relevance_rank: int = Field(ge=1, le=50)
    category: str
    section_path: str
    questions: str
    text: str
    source_key: str
    source_version: str
    archive: str
    line_start: int | None
    line_end: int | None


class PublicCitation(BaseModel):
    number: int
    chunk_id: str
    relevance_rank: int
    category: str
    section_path: str
    questions: str
    text: str
    source_key: str
    source_version: str


class SourceBundle(BaseModel):
    citations: list[PublicCitation]


class SourceAnchor(BaseModel):
    start: int | None
    end: int | None


class SourceDocumentView(BaseModel):
    source_key: str
    source_version: str
    section_path: str
    text: str
    anchor: SourceAnchor
