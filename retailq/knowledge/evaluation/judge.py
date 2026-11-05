import asyncio
from dataclasses import dataclass
import json
import warnings
from typing import Literal

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from retailq.knowledge.evaluation.metrics import faithfulness
from retailq.knowledge.rag_gateway import _unique_object


class Claim(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    text: str = Field(min_length=1)
    supported: bool
    citations: list[int]
    reason: str


class JudgeOutput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    claims: list[Claim]


@dataclass(frozen=True)
class JudgeResult:
    claims: tuple[dict, ...]
    score: float | None


def validate_judgement(value, answer, citations):
    if not isinstance(value,dict) or set(value)!={'claims'} or not isinstance(value['claims'],list):
        raise ValueError('invalid judge result')
    allowed={c.number for c in citations}
    for claim in value['claims']:
        if not isinstance(claim,dict) or set(claim)!={'text','supported','citations','reason'}:
            raise ValueError('invalid judge claim')
        if (not isinstance(claim['text'],str) or not claim['text'].strip() or claim['text'] not in answer
                or type(claim['supported']) is not bool or not isinstance(claim['citations'],list)
                or any(type(n) is not int or n not in allowed for n in claim['citations'])
                or not isinstance(claim['reason'],str)):
            raise ValueError('invalid judge claim evidence')
        if claim['supported'] and not claim['citations']:
            raise ValueError('supported claim requires evidence')
        if not claim['supported'] and not claim['reason'].strip():
            raise ValueError('unsupported claim requires reason')
    return faithfulness(c['supported'] for c in value['claims'])


class FaithfulnessJudge:
    def __init__(self, settings=None, *, invoke=None):
        self.settings, self.invoke = settings, invoke
        self.http = self.http_async = self.model = None
        if invoke is None:
            self.http=httpx.Client(trust_env=False)
            self.http_async=httpx.AsyncClient(trust_env=False)
            self.model=ChatOpenAI(model=settings.openai_model,base_url=settings.openai_base_url,
                api_key=settings.openai_api_key,max_tokens=settings.max_output_tokens,
                timeout=settings.model_timeout_seconds,max_retries=0,use_responses_api=False,stream_usage=False,
                http_client=self.http,http_async_client=self.http_async)

    async def aclose(self):
        if self.http_async is not None: await self.http_async.aclose()
        if self.http is not None: self.http.close()

    async def judge(self, answer, citations):
        evidence=[{'number':c.number,'question':c.questions,
                   'answer':c.text,'section_path':getattr(c,'section_path','')} for c in citations]
        payload=json.dumps({'answer':answer,'evidence':evidence},ensure_ascii=False)
        messages=[SystemMessage(content='逐条提取回答中的事实断言，按提供的证据判断是否支持。只返回 JSON 对象：'
                   '{"claims":[{"text":"回答中的原句片段","supported":true或false,"citations":[证据编号],"reason":"缺证或冲突原因"}]}。'
                   '每个 text 必须是回答中逐字连续出现的原文片段，不得改写、补字、删字拼接或换同义词；'
                   '若一句包含多个相关条件，无法逐字拆分时保留整句，只要其中有无证据或冲突的部分就判为不支持。'
                   '支持的断言必须提供对应证据编号；不支持必须说明原因。不要推测证据外的事实。'),HumanMessage(content=payload)]
        if self.invoke is not None:
            value=await self.invoke(messages)
        else:
            with warnings.catch_warnings():
                warnings.filterwarnings('error',message='(?s).*Overriding to method=',category=UserWarning)
                structured=self.model.with_structured_output(JudgeOutput,method=self.settings.structured_output_method,include_raw=True)
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                result=await structured.ainvoke(messages)
            raw=result['raw']
            if (result.get('parsing_error') or result.get('parsed') is None or raw.tool_calls or
                    raw.additional_kwargs.get('tool_calls') or raw.response_metadata.get('finish_reason')!='stop'):
                raise ValueError('judge returned incomplete response')
            value=json.loads(raw.content,object_pairs_hook=_unique_object)
            if JudgeOutput.model_validate(value)!=JudgeOutput.model_validate(result['parsed']):
                raise ValueError('judge parser differs from raw response')
        parsed=JudgeOutput.model_validate(value).model_dump()
        score=validate_judgement(parsed,answer,citations)
        return JudgeResult(tuple(parsed['claims']),score)
