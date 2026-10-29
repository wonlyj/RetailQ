"""Buffered structured RAG output; never stream unvalidated drafts."""
import asyncio
import json
import warnings

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from retailq.errors import ServiceError
from retailq.knowledge.answer import RAGDecision, validate_decision


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class OpenAIRAGGateway:
    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.Client(trust_env=False)
        self.http_async = httpx.AsyncClient(trust_env=False)
        self.model = ChatOpenAI(model=settings.openai_model, base_url=settings.openai_base_url,
            api_key=settings.openai_api_key, max_tokens=settings.max_output_tokens,
            timeout=settings.model_timeout_seconds, max_retries=0, use_responses_api=False, stream_usage=False,
            http_client=self.http, http_async_client=self.http_async)

    async def aclose(self):
        await self.http_async.aclose()
        self.http.close()

    async def answer(self, bundle):
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings('error', message='(?s).*Overriding to method=', category=UserWarning)
                structured = self.model.with_structured_output(RAGDecision, method=self.settings.structured_output_method,
                                                               include_raw=True)
            messages = [(SystemMessage if role == 'system' else HumanMessage)(content=content)
                        for role, content in bundle.messages]
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                output = await structured.ainvoke(messages)
            raw = output['raw']
            if (output.get('parsing_error') or output.get('parsed') is None or raw.tool_calls or
                    raw.additional_kwargs.get('tool_calls') or raw.response_metadata.get('finish_reason') != 'stop'):
                raise ValueError('invalid structured response')
            strict = RAGDecision.model_validate(json.loads(raw.content, object_pairs_hook=_unique_object))
            parsed = RAGDecision.model_validate(output['parsed'])
            if strict != parsed:
                raise ValueError('response differs from parsed output')
            validate_decision(strict, bundle)
            return strict
        except ServiceError:
            raise
        except TimeoutError:
            raise ServiceError('upstream_timeout', 504, '模型响应超时，请稍后重试。') from None
        except Exception:
            raise ServiceError('rag_answer_invalid', 502, '模型未返回完整有效的知识回答，请重试。') from None
