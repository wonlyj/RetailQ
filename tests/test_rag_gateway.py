import json
from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage

from retailq.errors import ServiceError
from retailq.knowledge.answer import RAGDecision, prepare_bundle
from retailq.knowledge.rag_gateway import OpenAIRAGGateway
from tests.test_rag_answer import retrieval

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize('content,finish', [('{"useful":true', 'stop'), ('{}', 'length')])
async def test_gateway_rejects_truncated_or_bad_finish(content, finish):
    raw = AIMessage(content=content, response_metadata={'finish_reason':finish})
    class Structured:
        async def ainvoke(self, messages):
            return {'raw':raw, 'parsed':RAGDecision(useful=True, answer='答案[1]', reason='', citations=[1]), 'parsing_error':None}
    class Model:
        def with_structured_output(self, schema, *, method, include_raw):
            assert include_raw and method == 'json_mode'
            return Structured()
    gateway = OpenAIRAGGateway.__new__(OpenAIRAGGateway)
    gateway.model = Model()
    gateway.settings = SimpleNamespace(structured_output_method='json_mode', model_timeout_seconds=5)
    bundle = prepare_bundle(retrieval(1), system_prompt='约束', input_budget=50000)
    with pytest.raises(ServiceError):
        await gateway.answer(bundle)


async def test_gateway_validates_strict_raw_and_citations():
    expected = RAGDecision(useful=True, answer='规则为10元[1]。', reason='', citations=[1])
    raw = AIMessage(content=json.dumps(expected.model_dump(), ensure_ascii=False), response_metadata={'finish_reason':'stop'})
    class Structured:
        async def ainvoke(self, messages): return {'raw':raw, 'parsed':expected, 'parsing_error':None}
    class Model:
        def with_structured_output(self, schema, *, method, include_raw): return Structured()
    gateway = OpenAIRAGGateway.__new__(OpenAIRAGGateway)
    gateway.model = Model()
    gateway.settings = SimpleNamespace(structured_output_method='json_mode', model_timeout_seconds=5)
    bundle = prepare_bundle(retrieval(1), system_prompt='约束', input_budget=50000)
    assert await gateway.answer(bundle) == expected


async def test_gateway_rejects_tool_call_in_structured_response():
    expected = RAGDecision(useful=True, answer='规则为10元[1]。', reason='', citations=[1])
    raw = AIMessage(content=json.dumps(expected.model_dump(), ensure_ascii=False),
                    additional_kwargs={'tool_calls':[{'id':'fake'}]}, response_metadata={'finish_reason':'stop'})
    class Structured:
        async def ainvoke(self, messages): return {'raw':raw, 'parsed':expected, 'parsing_error':None}
    class Model:
        def with_structured_output(self, schema, *, method, include_raw): return Structured()
    gateway = OpenAIRAGGateway.__new__(OpenAIRAGGateway)
    gateway.model = Model()
    gateway.settings = SimpleNamespace(structured_output_method='json_mode', model_timeout_seconds=5)
    bundle = prepare_bundle(retrieval(1), system_prompt='约束', input_budget=50000)
    with pytest.raises(ServiceError):
        await gateway.answer(bundle)


async def test_gateway_reports_connection_failure_separately_from_bad_json():
    import httpx
    import openai
    class Structured:
        async def ainvoke(self,messages):
            raise openai.APIConnectionError(request=httpx.Request('POST','https://example.test'))
    class Model:
        def with_structured_output(self,schema,*,method,include_raw): return Structured()
    gateway=OpenAIRAGGateway.__new__(OpenAIRAGGateway)
    gateway.model=Model()
    gateway.settings=SimpleNamespace(structured_output_method='json_mode',model_timeout_seconds=5)
    bundle=prepare_bundle(retrieval(1),system_prompt='约束',input_budget=50000)
    with pytest.raises(ServiceError) as exc:
        await gateway.answer(bundle)
    assert exc.value.code=='upstream_unavailable'
