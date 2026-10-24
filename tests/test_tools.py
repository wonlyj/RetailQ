import asyncio
import json
from unittest.mock import Mock

import pytest
from langchain_core.tools import tool

from retailq.config import Settings


def registry_for(repo=None, *, timeout=0.05):
    from retailq.tools.runtime import ToolContext, build_registry
    return build_registry(repo or Mock(), ToolContext(1, 'alice', 'call_1'),
                          Settings(_env_file=None, tool_timeout_seconds=timeout))


def test_exact_five_langchain_tools():
    registry = registry_for()
    assert {t.name for t in registry.tools} == {'query_order', 'query_product', 'query_logistics', 'query_faq', 'create_ticket'}
    for t in registry.tools:
        assert not {'user_id', 'conversation_id', 'tool_call_id'} & set(t.args)
    with pytest.raises(ValueError):
        registry.register(registry.tools[0], retry_safe=True)


@pytest.mark.anyio
async def test_invalid_unknown_and_mock_results():
    registry = registry_for()
    for args in [{'order_id': 1001}, {'order_id': ''}, {'order_id': '1', 'user_id': 'forged'}]:
        result = await registry.execute({'name': 'query_order', 'args': args, 'id': 'call_1'})
        assert result.payload['error']['code'] == 'invalid_arguments'
    assert (await registry.execute({'name':'other','args':{},'id':'call_1'})).payload['error']['code'] == 'unknown_tool'
    for name, key in [('query_order','order_id'),('query_product','product_id'),('query_logistics','order_id')]:
        outcome = await registry.execute({'name':name,'args':{key:'001001'},'id':'call_1'})
        assert outcome.payload['ok'] is True
        assert outcome.payload['data'][key] == '001001'
        assert outcome.payload['data']['source'] == 'demo'


@pytest.mark.anyio
async def test_read_timeout_retries_once_and_schema_error_never_runs():
    count = 0
    @tool
    async def slow(value: str) -> dict:
        """A controlled slow read."""
        nonlocal count
        count += 1
        await asyncio.sleep(1)
        return {}
    registry = registry_for(timeout=0.1)
    registry.register(slow, retry_safe=True)
    result = await registry.execute({'name':'slow','args':{'value':'x'},'id':'call_1'})
    assert result.payload['error']['code'] == 'tool_timeout'
    assert count == 2
    await registry.execute({'name':'slow','args':{},'id':'call_1'})
    assert count == 2


@pytest.mark.anyio
async def test_execution_errors_are_redacted_and_not_retried_when_permanent():
    count = 0
    @tool
    async def bad() -> dict:
        """A controlled permanent error."""
        nonlocal count
        count += 1
        raise ValueError('password-secret')
    registry = registry_for()
    registry.register(bad, retry_safe=True)
    outcome = await registry.execute({'name':'bad','args':{},'id':'call_1'})
    assert count == 1
    assert outcome.payload['ok'] is False
    assert 'password-secret' not in json.dumps(outcome.payload)


@pytest.mark.mysql
@pytest.mark.anyio
async def test_faq_and_late_ticket_commit(test_database, monkeypatch):
    from retailq.repositories import ChatRepository
    from retailq.tools.runtime import ToolContext, build_registry
    import time
    repo = ChatRepository(test_database)
    cid = repo.start_turn('alice', None, [], '配送延迟请投诉')
    repo.append_message(cid,'alice','assistant',None,[{'id':'call_1','type':'function','function':{'name':'create_ticket','arguments':'{}'}}])
    original = repo.create_ticket
    attempts = 0
    def slow(*args):
        nonlocal attempts
        attempts += 1
        time.sleep(0.08)
        return original(*args)
    monkeypatch.setattr(repo,'create_ticket',slow)
    registry = build_registry(repo, ToolContext(cid,'alice','call_1'), Settings(_env_file=None,tool_timeout_seconds=0.01))
    unknown = await registry.execute({'name':'create_ticket','args':{'description':'配送延迟','ticket_type':'投诉'},'id':'call_1'})
    assert unknown.payload['error']['code'] == 'outcome_unknown'
    assert unknown.receipt_state == 'pending'
    await registry.drain()
    records = repo.load_messages(cid,'alice')
    receipts = [r for r in records if r.role=='tool']
    assert attempts == 1 and len(receipts) == 1
    assert json.loads(receipts[0].content)['ok'] is True
    registry.settings = Settings(_env_file=None, tool_timeout_seconds=1)
    hit = await registry.execute({'name':'query_faq','args':{'keyword':'退货政策'},'id':'faq_1'})
    miss = await registry.execute({'name':'query_faq','args':{'keyword':'邮费'},'id':'faq_2'})
    assert hit.payload['data']['found'] is True
    assert miss.payload['data'] == {'found':False,'items':[]}
