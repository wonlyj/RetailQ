import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceRef(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_key: str = Field(min_length=1, max_length=255)
    source_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    section_path: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    eval_id: str = Field(pattern=r'^[A-E][0-9]{2}$')
    bucket: Literal['A_policy','B_model','C_colloquial','D_missing','E_multi']
    difficulty: Literal['easy','medium','hard']
    query: str = Field(min_length=1, max_length=512)
    filters: dict[str,str]
    answerable: bool
    required_facts: list[str] = Field(min_length=1)
    relevant_groups: list[list[SourceRef]]
    forbidden_claims: list[str]
    family_id: str = Field(min_length=1, max_length=128)
    split: Literal['calibration','test']

    @model_validator(mode='after')
    def validate_evidence(self):
        if self.eval_id[0] != self.bucket[0] or self.answerable != bool(self.relevant_groups):
            raise ValueError('answerability and evidence mismatch')
        if self.answerable and (len(self.relevant_groups) != len(self.required_facts) or any(not g for g in self.relevant_groups)):
            raise ValueError('each required fact needs evidence')
        if self.bucket == 'D_missing' and self.answerable:
            raise ValueError('missing case cannot be answerable')
        if set(self.filters) - {'category','category_prefix','content_type'}:
            raise ValueError('unknown retrieval filter')
        if 'category' in self.filters and 'category_prefix' in self.filters:
            raise ValueError('incompatible category filters')
        return self


def read_cases(path: Path) -> tuple[EvalCase, ...]:
    try:
        rows = tuple(EvalCase.model_validate_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip())
        expected = {(b,s):n for b in ('A_policy','B_model','C_colloquial','D_missing','E_multi') for s,n in (('calibration',4),('test',16))}
        if len(rows)!=100 or len({c.eval_id for c in rows})!=100 or Counter((c.bucket,c.split) for c in rows)!=expected:
            raise ValueError('case inventory invalid')
        family_splits=defaultdict(set)
        for case in rows:
            family_splits[case.family_id].add(case.split)
        if any(len(splits)!=1 for splits in family_splits.values()):
            raise ValueError('family crosses evaluation splits')
        return rows
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('invalid evaluation cases') from exc


@dataclass(frozen=True)
class MappedCase:
    case: EvalCase
    groups: tuple[frozenset[int], ...]


def load_cases(path: Path, snapshot, *, repository) -> tuple[MappedCase, ...]:
    from retailq.knowledge.corpus import is_active
    cases=read_cases(path)
    records=repository.get_chunks(list(snapshot.active_ids))
    sources={source.key:source for source in snapshot.sources}
    mapped=[]
    for case in cases:
        groups=[]
        for group in case.relevant_groups:
            matches=set()
            for ref in group:
                source=sources.get(ref.source_key)
                if source is None or source.digest != ref.source_version:
                    raise ValueError(f'{case.eval_id}: source version not in frozen corpus')
                for chunk_id in source.chunk_ids:
                    row=records.get(chunk_id)
                    if row is None or row.vectorize_status!='done' or row.vector_id!=str(chunk_id) or not is_active(row,snapshot):
                        continue
                    parts=(row.draft.section_path or '').split(' / ')
                    if len(parts)>=3 and parts[1]==ref.section_path and ref.quote in row.draft.answer:
                        if case.filters.get('category') and row.draft.category!=case.filters['category']:
                            continue
                        if case.filters.get('content_type') and row.draft.content_type!=case.filters['content_type']:
                            continue
                        if case.filters.get('category_prefix') and not (row.draft.category==case.filters['category_prefix'] or row.draft.category.startswith(case.filters['category_prefix']+' / ')):
                            continue
                        matches.add(chunk_id)
            if not matches:
                raise ValueError(f'{case.eval_id}: evidence not found in frozen chunks')
            groups.append(frozenset(matches))
        mapped.append(MappedCase(case,tuple(groups)))
    return tuple(mapped)
