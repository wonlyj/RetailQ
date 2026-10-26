from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from retailq.errors import ServiceError
from retailq.gateway import OpenAIModelGateway
from retailq.tools.runtime import ToolContext, build_registry
from tests.support.servers import run_server
from tests.support.tool_upstream import ControlledToolUpstream, wire_call
from tests.support.upstream import ControlledUpstream
from tests.test_gateway import settings_for


@pytest.mark.anyio
async def test_select_then_final_wire_contract():
    upstream = ControlledToolUpstream()
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        tools = build_registry(Mock(),ToolContext(1,'alice'),gateway.settings).tools
        try:
            messages = [HumanMessage('订单1001物流')]
            selection = await gateway.select_tool(messages,tools)
            assert selection.tool_calls[0]['args'] == {'order_id':'1001'}
            result = ToolMessage(content='{"ok":true,"data":{"status":"派送中"}}',tool_call_id='call_1')
            chunks = [c async for c in gateway.stream([*messages,selection,result])]
            assert len([c for c in chunks if c.text]) == 2
            first, final = [r[0] for r in upstream.requests]
            assert first['stream'] is False and first['parallel_tool_calls'] is False
            assert first['tool_choice'] == 'auto' and len(first['tools']) == 5
            assert 'tools' not in final and 'tool_choice' not in final
            assert final['messages'][-1]['tool_call_id'] == 'call_1'
            assert final['messages'][-2]['tool_calls'][0]['id'] == 'call_1'
        finally:
            await gateway.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize('case',['bad_json','missing_id','long_id','not_object','length','no_finish'])
async def test_bad_selection_is_rejected(case):
    call = wire_call()
    kwargs = {}
    if case == 'bad_json': call['function']['arguments'] = '{"order_id":'
    if case == 'missing_id': call['id'] = ''
    if case == 'long_id': call['id'] = 'x'*65
    if case == 'not_object': call['function']['arguments'] = '[]'
    if case == 'length': kwargs['selection_reason'] = 'length'
    if case == 'no_finish': kwargs['selection_reason'] = None
    upstream = ControlledToolUpstream([call],**kwargs)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            with pytest.raises(ServiceError):
                await gateway.select_tool([HumanMessage('物流')],build_registry(Mock(),ToolContext(1,'alice'),gateway.settings).tools)
            assert len(upstream.requests) == 1
        finally:
            await gateway.aclose()


@pytest.mark.anyio
async def test_dsml_in_selection_content_is_not_accepted_as_a_tool_call():
    upstream = ControlledUpstream(text='<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="query_faq">')
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            tools = build_registry(Mock(),ToolContext(1,'alice'),gateway.settings).tools
            with pytest.raises(ServiceError) as error:
                await gateway.select_tool([HumanMessage('运动鞋应该怎么清洁和存放')],tools)
            assert error.value.code == 'unsupported_tool_protocol'
            assert len(upstream.requests) == 1
        finally:
            await gateway.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize('chunks',[
    ['<｜｜DSML｜｜ calls>'],
    list('<｜｜DSML｜｜ calls>'),
    ['</｜｜DS','ML｜｜ invoke>'],
    ['<|DS','ML|calls>'],
    ['<｜DS','ML｜function_calls>'],
    ['正常前文。<','｜｜DSML','｜｜ calls>'],
])
async def test_dsml_stream_is_rejected_before_marker_fragments_are_emitted(chunks):
    upstream = ControlledUpstream(chunks=chunks)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        emitted = []
        try:
            with pytest.raises(ServiceError) as error:
                async for chunk in gateway.stream([HumanMessage('运动鞋应该怎么清洁和存放')]):
                    emitted.append(chunk.text)
            assert error.value.code == 'unsupported_tool_protocol'
            assert ''.join(emitted) in ('','正常前文。')
            assert upstream.closed.wait(2)
        finally:
            await gateway.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize('chunks,expected',[
    (['价格',' <',' 99；DSML只是普通文字。'],'价格 < 99；DSML只是普通文字。'),
    (['演示型号',' <'],'演示型号 <'),
    (['资料',' <','｜｜D','不是协议'],'资料 <｜｜D不是协议'),
])
async def test_protocol_guard_preserves_ordinary_text_and_unfinished_angle_brackets(chunks,expected):
    upstream = ControlledUpstream(chunks=chunks)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            output = [chunk async for chunk in gateway.stream([HumanMessage('商品资料')])]
            assert ''.join(chunk.text for chunk in output) == expected
            assert output[-1].finish_reason == 'stop'
        finally:
            await gateway.aclose()
