"""Strict console request DTOs and a whitelist serializer for safe read DTOs."""
from typing import Annotated, Literal
from dataclasses import asdict

from pydantic import Field, field_validator, model_validator

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ConversationSnapshot
from retailq.knowledge.review_contracts import (
    ChunkDetail, ConsoleSummary, FrozenRecord, GroupDetail, GroupSummary, LegacyRunDetail,
    OperationView, Page, Revision, RunDetail, RunSummary, Token, normalize_category, parse_db_id,
)


class RequestKeyBody(FrozenRecord):
    request_key: Token


class PrepareBody(RequestKeyBody):
    conversation_ids: Annotated[list[str], Field(min_length=1, max_length=100)] | None = None

    @field_validator('conversation_ids')
    @classmethod
    def ids(cls, values):
        if values is not None:
            ids = [parse_db_id(value) for value in values]
            if len(set(ids)) != len(ids):
                raise ValueError('duplicate conversation ID')
        return values


class DecisionBody(RequestKeyBody):
    expected_revision: Revision
    decision: Literal['approve','reject']
    category: str | None = None
    reason: Annotated[str, Field(max_length=512)] | None = None

    @field_validator('category')
    @classmethod
    def normalized(cls, value):
        return normalize_category(value) if value is not None else None

    @model_validator(mode='after')
    def rejection_has_no_category(self):
        if self.decision == 'reject' and self.category is not None:
            raise ValueError('rejection cannot submit a category')
        return self


class SealBody(RequestKeyBody):
    confirm_run_id: Token
    reason: Annotated[str, Field(max_length=512)] | None = None


def db_id(value):
    try:
        return parse_db_id(value)
    except ValueError:
        raise ServiceError('knowledge_id_invalid',422,'数据库ID必须是规范十进制字符串。') from None


DB_FIELDS = frozenset({'id','conversation_id','max_message_id','staging_id','chunk_id',
                       'target_chunk_id','planned_target_id','prev_chunk_id','next_chunk_id'})


def console_json(view):
    if not isinstance(view,(Page,ConsoleSummary,RunSummary,RunDetail,GroupSummary,GroupDetail,
                            ChunkDetail,OperationView,LegacyRunDetail,ConversationSnapshot)):
        raise TypeError('only safe console DTOs may cross the API boundary')
    def encode(value, name=''):
        if name in DB_FIELDS and value is not None:
            return None if name == 'max_message_id' and value == 0 else str(value)
        if name == 'message_ids':
            return [str(i) for i in value]
        if isinstance(value,dict):
            return {key:encode(item,key) for key,item in value.items()}
        if isinstance(value,(tuple,list)):
            return [encode(item) for item in value]
        return value
    return encode(asdict(view) if isinstance(view,ConversationSnapshot) else view.model_dump(mode='json'))
