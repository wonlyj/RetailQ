from dataclasses import asdict
import json
from pathlib import Path
import warnings

import httpx
from langchain_core.messages import HumanMessage,SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel,ConfigDict,Field

from retailq.config import Settings
from retailq.context import estimate_tokens,require_budget
from retailq.errors import ServiceError
from retailq.knowledge.contracts import ConversationSnapshot,QAProposal


class ExtractedItem(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source_ref: str = Field(min_length=1,max_length=255)
    question: str = Field(min_length=1,max_length=65535)
    answer: str = Field(min_length=1,max_length=65535)
    evidence: str = Field(min_length=1,max_length=65535)


class ExtractionResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    items: list[ExtractedItem] = Field(max_length=100)


def prompt(name):
    return (Path(__file__).resolve().parents[1]/'prompts'/name).read_text(encoding='utf-8')


def messages_for(schema,system,data):
    system=system+'\n必须返回单一JSON对象。严格遵守以下JSON Schema：\n'+json.dumps(schema.model_json_schema(),ensure_ascii=False)
    return [SystemMessage(system),HumanMessage(json.dumps(data,ensure_ascii=False))]


def extraction_cost(batch: list[ConversationSnapshot]) -> int:
    messages=messages_for(ExtractionResult,prompt('qa_extract.txt'),[asdict(row) for row in batch])
    # Covers schema in both instructions and json_schema response_format.
    return estimate_tokens(messages)+len(json.dumps(ExtractionResult.model_json_schema(),ensure_ascii=False).encode('utf-8'))+64


class OpenAIQAGateway:
    def __init__(self,settings: Settings,*,transport: httpx.BaseTransport | None=None):
        self.settings=settings
        self.http=httpx.Client(trust_env=False,transport=transport)
        self.model=ChatOpenAI(model=settings.openai_model,base_url=settings.openai_base_url,api_key=settings.openai_api_key,
            timeout=settings.model_timeout_seconds,max_tokens=settings.max_output_tokens,max_retries=0,
            use_responses_api=False,stream_usage=False,http_client=self.http)

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def close(self) -> None:self.http.close()

    def _invoke(self,schema,system,data):
        try:
            messages=messages_for(schema,system,data)
            cost=estimate_tokens(messages)+len(json.dumps(schema.model_json_schema(),ensure_ascii=False).encode('utf-8'))+64
            require_budget(cost,self.settings.input_budget)
            with warnings.catch_warnings():
                warnings.filterwarnings('error',message='(?s).*Overriding to method=',category=UserWarning)
                structured=self.model.with_structured_output(schema,method=self.settings.structured_output_method,include_raw=True)
            output=structured.invoke(messages)
            raw=output['raw']
            if output.get('parsing_error') or output.get('parsed') is None or raw.tool_calls or raw.additional_kwargs.get('tool_calls') or raw.response_metadata.get('finish_reason')!='stop':
                raise ValueError('incomplete structured output')
            strict=schema.model_validate(json.loads(raw.content))
            if strict!=output['parsed']:raise ValueError('raw and parsed output differ')
            return strict
        except ServiceError:raise
        except Exception:
            raise ServiceError('knowledge_qa_invalid',502,'知识抽取或去重模型未返回完整有效结构，请重试或核查配置。') from None

    def extract(self,batch: list[ConversationSnapshot]) -> list[QAProposal]:
        if not batch:return []
        output=self._invoke(ExtractionResult,prompt('qa_extract.txt'),[asdict(row) for row in batch])
        return [QAProposal(**row.model_dump()) for row in output.items]
