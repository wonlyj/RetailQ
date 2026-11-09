"""The workflow is the authority over routes; the agent controls read-only steps."""
import asyncio
import json
import logging
import time

from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph,START,END
from retailq.api.source_schemas import CitationRecord
from retailq.services.chat import ChatEvent
from retailq.workflow.classification import COMPLAINT_REPLY,SMALLTALK_REPLY
from retailq.errors import ServiceError
from retailq.workflow.contracts import WorkflowState,TurnRef
from retailq.workflow.evidence import validate_answer_references
from retailq.workflow.sql import write_sql

logger=logging.getLogger('retailq.workflow')


def emit(event):
    get_stream_writer()({'event':event.event,'data':event.data})


def build_graph(agent,classifier,evidence,repository,checkpointer,*,query_steps=None,order_steps=None):
    graph=StateGraph(WorkflowState)

    async def resolve(state):
        if query_steps is None:raise ServiceError('router_unavailable',503,'分流器尚未就绪。')
        return await query_steps.resolve(state)
    async def intent(state): return await classifier.classify(state)
    async def route(state): return {}
    async def retrieve(state): return await evidence.retrieve(state)
    async def gate(state): return await evidence.gate(state)
    async def decide(state):
        patch=await agent.decide(state)
        if state.get('route')=='core' and state.get('intent')=='退款退货' and patch.get('status')=='answer':
            patch['actions']=list(dict.fromkeys([*patch.get('actions',[]),'ticket']))
        return patch
    async def tools(state): return await agent.execute(state)
    async def resolve_order(state):return await order_steps.resolve_order(state)
    async def select_order(state):return await order_steps.select_order(state)
    async def load_order(state):return await order_steps.load_order(state)
    async def plan_query(state):return await query_steps.plan(state)
    async def expand_query(state):return await query_steps.expand(state)
    async def retrieve_policy(state):return await evidence.retrieve_policy(state)
    async def cancelled(state):
        turn=TurnRef(state['turn_id'],state['conversation_id'],state['user_message_id'])
        await write_sql(repository.cancel_pending,turn,state['user_id'],state.get('order_selection',{}).get('interrupt_id'))
        return {'status':'cancelled'}
    async def answer(state):
        patch=await agent.answer(state)
        if state.get('evidence',{}).get('citations') and patch['status']=='completed':
            validate_answer_references(patch['answer'],tuple(CitationRecord.model_validate(c) for c in state['evidence']['citations']))
        return patch
    async def complaint(state):
        emit(ChatEvent('token',{'text':COMPLAINT_REPLY}))
        return {'answer':COMPLAINT_REPLY,'actions':['handoff','ticket'],'status':'completed','finish_reason':'stop'}
    async def smalltalk(state):
        emit(ChatEvent('token',{'text':SMALLTALK_REPLY}))
        return {'answer':SMALLTALK_REPLY,'actions':[],'status':'completed','finish_reason':'stop'}
    async def fallback(state):
        emit(ChatEvent('token',{'text':state['answer']}))
        return {'status':'completed'}
    async def log(state):
        turn=TurnRef(state['turn_id'],state['conversation_id'],state['user_message_id'])
        if state.get('status')=='interrupted':
            await write_sql(repository.mark_terminal,turn,'interrupted',state.get('finish_reason'))
            return {}
        final=await write_sql(repository.finalize,turn,state)
        return {'final_message_id':final.message_id}

    def wrapped(name,fn):
        async def node(state):
            start=time.monotonic()
            entry={'node':name,'phase':'enter','turn_id':state.get('turn_id')}
            emit(ChatEvent('workflow',entry))
            working={**state,'trace':[*state.get('trace',[]),entry]}
            patch=await fn(working)
            exit_event={'node':name,'phase':'exit','turn_id':state.get('turn_id'),
                        'elapsed_ms':round((time.monotonic()-start)*1000,2)}
            emit(ChatEvent('workflow',exit_event))
            logger.info(json.dumps({**exit_event,'conversation_id':state.get('conversation_id'),
                'intent':patch.get('intent',state.get('intent')),'route':patch.get('route',state.get('route')),
                'status':patch.get('status',state.get('status'))},ensure_ascii=False))
            return {**patch,'trace':[*working['trace'],exit_event]}
        return node

    for name,fn in [('resolve',resolve),('intent',intent),('route',route),('retrieve',retrieve),('gate',gate),
                    ('agent_decide',decide),('tools',tools),('answer',answer),('complaint',complaint),
                    ('smalltalk',smalltalk),('fallback',fallback),('log',log),('resolve_order',resolve_order),
                    ('select_order',select_order),('load_order',load_order),('plan_query',plan_query),
                    ('expand_query',expand_query),('retrieve_policy',retrieve_policy),('cancelled',cancelled)]:
        graph.add_node(name,wrapped(name,fn))
    graph.add_edge(START,'resolve');graph.add_edge('resolve','intent');graph.add_edge('intent','route')
    graph.add_conditional_edges('route',lambda s:s['route'],{'knowledge':'retrieve','business':'agent_decide',
                                                          'complaint':'complaint','smalltalk':'smalltalk',
                                                          'core':'resolve_order','other':'agent_decide'})
    graph.add_conditional_edges('resolve_order',lambda s:'load_order' if s.get('order_id') else 'select_order',
                                {'load_order':'load_order','select_order':'select_order'})
    graph.add_conditional_edges('select_order',lambda s:'cancelled' if s.get('status')=='cancelled' else 'load_order',
                                {'cancelled':'cancelled','load_order':'load_order'})
    graph.add_edge('cancelled',END)
    graph.add_edge('load_order','plan_query')
    graph.add_conditional_edges('plan_query',lambda s:'expand_query' if s['query_plan']['core_case'] else 'retrieve_policy',
                                {'expand_query':'expand_query','retrieve_policy':'retrieve_policy'})
    graph.add_edge('expand_query','retrieve_policy');graph.add_edge('retrieve_policy','gate')
    graph.add_edge('retrieve','gate')
    graph.add_conditional_edges('gate',lambda s:'agent_decide' if s['gate']['allowed'] else 'fallback',
                                {'agent_decide':'agent_decide','fallback':'fallback'})
    graph.add_conditional_edges('agent_decide',lambda s:s['status'],{'tool':'tools','answer':'answer','limited':'log'})
    graph.add_edge('tools','agent_decide')
    for name in ('answer','complaint','smalltalk','fallback'):graph.add_edge(name,'log')
    graph.add_edge('log',END)
    return graph.compile(checkpointer=checkpointer)
