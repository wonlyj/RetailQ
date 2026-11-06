"""The workflow is the authority over routes; the agent controls read-only steps."""
import asyncio
import json
import logging
import time

from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph,START,END
from retailq.api.source_schemas import CitationRecord
from retailq.services.chat import ChatEvent
from retailq.workflow.classification import COMPLAINT_REPLY,SMALLTALK_REPLY,resolve_reference
from retailq.workflow.contracts import WorkflowState,TurnRef
from retailq.workflow.evidence import validate_answer_references
from retailq.workflow.sql import write_sql

logger=logging.getLogger('retailq.workflow')


def emit(event):
    get_stream_writer()({'event':event.event,'data':event.data})


def build_graph(agent,classifier,evidence,repository,checkpointer):
    graph=StateGraph(WorkflowState)

    async def resolve(state): return {'resolved_question':resolve_reference(state['raw_question'])}
    async def intent(state): return await classifier.classify(state)
    async def route(state): return {}
    async def retrieve(state): return await evidence.retrieve(state)
    async def gate(state): return await evidence.gate(state)
    async def decide(state): return await agent.decide(state)
    async def tools(state): return await agent.execute(state)
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
                    ('smalltalk',smalltalk),('fallback',fallback),('log',log)]:
        graph.add_node(name,wrapped(name,fn))
    graph.add_edge(START,'resolve');graph.add_edge('resolve','intent');graph.add_edge('intent','route')
    graph.add_conditional_edges('route',lambda s:s['route'],{'knowledge':'retrieve','business':'agent_decide',
                                                          'complaint':'complaint','smalltalk':'smalltalk'})
    graph.add_edge('retrieve','gate')
    graph.add_conditional_edges('gate',lambda s:'agent_decide' if s['gate']['allowed'] else 'fallback',
                                {'agent_decide':'agent_decide','fallback':'fallback'})
    graph.add_conditional_edges('agent_decide',lambda s:s['status'],{'tool':'tools','answer':'answer','limited':'log'})
    graph.add_edge('tools','agent_decide')
    for name in ('answer','complaint','smalltalk','fallback'):graph.add_edge(name,'log')
    graph.add_edge('log',END)
    return graph.compile(checkpointer=checkpointer)
