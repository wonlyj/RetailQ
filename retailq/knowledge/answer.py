"""Immutable evidence snapshots, input budgeting, and answer validation."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from retailq.context import estimate_tokens, require_budget
from retailq.errors import ServiceError


class RAGDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    useful: bool
    answer: str
    reason: str
    citations: list[int] = Field(default_factory=list)


@dataclass(frozen=True)
class CitationSnapshot:
    number: int
    chunk_id: int
    relevance_rank: int
    category: str
    section_path: str
    questions: str
    text: str
    source_key: str
    source_version: str
    archive: str
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True)
class EvidenceBundle:
    question: str
    strategy: str
    corpus_hash: str
    citations: tuple[CitationSnapshot, ...]
    messages: tuple[tuple[str, str], ...]
    bundle_hash: str


@dataclass(frozen=True)
class ConfidencePolicy:
    identity: dict
    thresholds: dict

    @classmethod
    def load(cls, path, *, expected_identity):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            keys = {'model', 'corpus_hash', 'prompt_hash', 'query_hash', 'strategy'}
            if (not isinstance(expected_identity, dict) or set(expected_identity) != keys or
                    any(not isinstance(v, str) or not v for v in expected_identity.values()) or
                    set(data) != {'identity', 'calibrated', 'thresholds'} or data['identity'] != expected_identity or data['calibrated'] is not True):
                raise ValueError('uncalibrated or mismatched')
            thresholds = data['thresholds']
            if not isinstance(thresholds, dict) or not thresholds or any(type(v) not in (int, float) or not math.isfinite(v) for v in thresholds.values()):
                raise ValueError('invalid thresholds')
            return cls(data['identity'], thresholds)
        except (OSError, ValueError, TypeError, KeyError):
            raise ServiceError('rag_policy_unavailable', 503, '知识置信度配置缺失或与当前版本不一致，请先完成校准。') from None

    def allows(self, result):
        if result.strategy != self.identity['strategy'] or result.strategy not in self.thresholds or result.snapshot.corpus_hash != self.identity['corpus_hash']:
            raise ServiceError('rag_policy_unavailable', 503, '当前检索策略尚未校准。')
        return bool(result.candidates) and result.candidates[0].score >= self.thresholds[result.strategy]


def _citation(item, snapshot, number):
    source = next((s for s in snapshot.sources if item.id in s.chunk_ids), None)
    location = next((row for row in source.line_ranges if row[0] == item.id), None) if source else None
    if (source is not None and location is None) or (source is None and item.id not in snapshot.approved_ids):
        raise ServiceError('knowledge_corpus_invalid', 409, '知识来源定位不完整，请核查。')
    return CitationSnapshot(number, item.id, item.rank, item.draft.category, item.draft.section_path or '审核知识',
        item.draft.questions, item.draft.answer, source.key if source else 'review', source.digest if source else '',
        source.archive if source else '', location[1] if location else None, location[2] if location else None)


def prepare_bundle(result, *, system_prompt: str, input_budget: int) -> EvidenceBundle:
    schema_cost = len(json.dumps(RAGDecision.model_json_schema(), ensure_ascii=False).encode('utf-8')) + 64
    selected = list(result.candidates)
    while selected:
        ordered = selected[::2] + list(reversed(selected[1::2]))
        citations = tuple(_citation(item, result.snapshot, i) for i, item in enumerate(ordered, 1))
        payload = {'question': result.understanding.raw_question, 'evidence': [asdict(c) for c in citations]}
        messages = (('system', system_prompt), ('human', json.dumps(payload, ensure_ascii=False, separators=(',', ':'))))
        estimated = estimate_tokens([SystemMessage(content=messages[0][1]), HumanMessage(content=messages[1][1])]) + schema_cost
        if estimated <= input_budget:
            digest = sha256(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            return EvidenceBundle(result.understanding.raw_question, result.strategy, result.snapshot.corpus_hash,
                                  citations, messages, digest)
        selected.pop()
    require_budget(input_budget + 1, input_budget)
    raise AssertionError('unreachable')


def validate_decision(decision: RAGDecision, bundle: EvidenceBundle) -> None:
    references = [int(n) for n in re.findall(r'\[(\d+)\]', decision.answer)]
    allowed = {c.number for c in bundle.citations}
    valid = (len(set(decision.citations)) == len(decision.citations) and set(decision.citations) == set(references)
             and set(references) <= allowed)
    if decision.useful:
        valid &= bool(decision.answer.strip() and references)
    else:
        valid &= bool(decision.reason.strip()) and not decision.answer.strip() and not decision.citations
    if not valid:
        raise ServiceError('rag_answer_invalid', 502, '模型答复或引用格式无效，请重试。')
