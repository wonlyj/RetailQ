"""MySQL facts and atomic finalization, independent of graph checkpoint storage."""
from hashlib import sha256
import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from retailq.api.source_schemas import CitationRecord
from retailq.context import history_turns
from retailq.errors import ServiceError
from retailq.models import Conversation,Message
from retailq.repositories import ChatRepository,MessageRecord,database_errors
from retailq.knowledge.rag_models import LowConfidenceQuestion
from retailq.workflow.contracts import FinalizedTurn,TurnRef,TicketActionResult
from retailq.workflow.models import WorkflowAction,WorkflowTurn


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def action_view(action):
    return {'action_id':action.action_id,'kind':action.kind,'status':action.status,
            'origin_assistant_message_id':str(action.origin_assistant_message_id),'result':action.result}


class WorkflowRepository:
    def __init__(self,database): self.database = database

    @database_errors
    def start_turn(self,body):
        with self.database.sessions.begin() as session:
            cid = body.conversation_id
            if cid is None:
                conversation = Conversation(user_id=body.user_id)
                session.add(conversation); session.flush(); cid = conversation.id
                for item in body.history: session.add(Message(conversation_id=cid,role=item.role,content=item.content))
            else: ChatRepository._owned(session,cid,body.user_id,lock=True)
            user = Message(conversation_id=cid,role='user',content=body.message)
            session.add(user); session.flush()
            turn = WorkflowTurn(turn_id=uuid4().hex,conversation_id=cid,user_message_id=user.id,payload={})
            session.add(turn); session.flush()
            return TurnRef(turn.turn_id,cid,user.id)

    def _turn(self,session,turn,user_id,*,lock=True):
        row = session.scalar(select(WorkflowTurn).where(WorkflowTurn.turn_id==turn.turn_id).with_for_update())
        if row is None or row.conversation_id!=turn.conversation_id or row.user_message_id!=turn.user_message_id:
            raise ServiceError('workflow_turn_not_found',404,'工作流轮次不存在。')
        if user_id is not None: ChatRepository._owned(session,row.conversation_id,user_id,lock=lock)
        return row

    @database_errors
    def get_turn(self,turn_id,user_id):
        with self.database.sessions() as session:
            row = session.get(WorkflowTurn,turn_id)
            if row is None: raise ServiceError('workflow_turn_not_found',404,'工作流轮次不存在。')
            ChatRepository._owned(session,row.conversation_id,user_id)
            return {'turn_id':row.turn_id,'conversation_id':row.conversation_id,'user_message_id':row.user_message_id,
                    'final_message_id':row.final_message_id,'status':row.status,'payload':row.payload}

    @database_errors
    def load_projection(self,cid,user_id,*,before_message_id=None):
        with self.database.sessions() as session:
            ChatRepository._owned(session,cid,user_id)
            statement = select(Message).where(Message.conversation_id==cid).order_by(Message.id)
            if before_message_id is not None: statement=statement.where(Message.id<before_message_id)
            rows = session.scalars(statement).all()
            actions = session.scalars(select(WorkflowAction).join(WorkflowTurn).where(WorkflowTurn.conversation_id==cid)).all()
            by_request = {a.request_message_id:a for a in actions if a.request_message_id is not None}
            action_call_ids = {a.tool_call_id for a in actions}
            records = []
            for row in rows:
                if row.id in by_request:
                    action=by_request[row.id]
                    text = ('工单工具已确认：'+json.dumps(action.result,ensure_ascii=False) if action.status=='completed'
                            else '工单创建结果尚未确认，请核查后再操作。')
                    records.append(MessageRecord(row.id,cid,'assistant',
                        f'用户通过按钮确认建工单（原回复{action.origin_assistant_message_id}，动作{action.action_id}）。{text}',
                        None,None,row.created_at))
                elif row.role=='tool' and row.tool_call_id in action_call_ids: continue
                else: records.append(MessageRecord(row.id,cid,row.role,row.content,row.tool_calls,row.tool_call_id,row.created_at))
            return history_turns(records)

    @database_errors
    def record_tool_request(self,turn,selection):
        if len(selection.tool_calls)!=1: raise ValueError('one tool per step required')
        call = selection.tool_calls[0]
        wire = [{'id':call['id'],'type':'function','function':{'name':call['name'],
                'arguments':json.dumps(call['args'],ensure_ascii=False)}}]
        with self.database.sessions.begin() as session:
            row=self._turn(session,turn,None)
            payload={**row.payload}; requests={**payload.get('tool_requests',{})}
            if call['id'] in requests:
                prior=session.get(Message,requests[call['id']])
                if prior.tool_calls!=wire: raise ServiceError('workflow_tool_conflict',409,'工具申请身份冲突。')
                return prior.id
            # IDs must also be unique across prior turns in this conversation.
            old=session.scalars(select(Message).where(Message.conversation_id==turn.conversation_id,Message.tool_calls.is_not(None))).all()
            if any(c.get('id')==call['id'] for m in old for c in (m.tool_calls or [])):
                raise ServiceError('reused_tool_call_id',502,'工具申请ID已使用，未执行。')
            message=Message(conversation_id=turn.conversation_id,role='assistant',content=selection.content or None,tool_calls=wire)
            session.add(message);session.flush()
            requests[call['id']]=message.id; payload['tool_requests']=requests; row.payload=payload
            return message.id

    @database_errors
    def record_tool_result(self,turn,call_id,payload):
        with self.database.sessions.begin() as session:
            row=self._turn(session,turn,None)
            if call_id not in row.payload.get('tool_requests',{}): raise ServiceError('missing_tool_request',409,'缺少工具申请。')
            existing=session.scalar(select(Message).where(Message.conversation_id==turn.conversation_id,
                Message.role=='tool',Message.tool_call_id.collate('utf8mb4_0900_bin')==call_id))
            content=json.dumps(payload,ensure_ascii=False)
            if existing:
                if json.loads(existing.content)!=payload: raise ServiceError('workflow_tool_conflict',409,'工具回执身份冲突。')
                return existing.id
            message=Message(conversation_id=turn.conversation_id,role='tool',content=content,tool_call_id=call_id)
            session.add(message);session.flush();return message.id

    def _create_actions(self,session,row,message_id,actions):
        for kind in actions:
            identity=uuid4().hex
            session.add(WorkflowAction(action_id=identity,turn_id=row.turn_id,origin_assistant_message_id=message_id,
                                       kind=kind,tool_call_id='wa_'+identity))
        session.flush()

    @database_errors
    def finalize(self,turn,state):
        final={key:state.get(key) for key in ('answer','status','actions','gate','evidence')}
        if not isinstance(final['answer'],str) or not final['answer'].strip() or final['status'] not in ('completed','limited'):
            raise ValueError('incomplete workflow answer')
        actions=final['actions'] or []
        if not isinstance(actions,list) or any(a not in ('handoff','ticket') for a in actions) or len(set(actions))!=len(actions):
            raise ValueError('invalid actions')
        final_hash=digest(final)
        with self.database.sessions.begin() as session:
            row=self._turn(session,turn,state['user_id'])
            if row.final_message_id is not None:
                if row.payload.get('final_hash')!=final_hash:
                    raise ServiceError('workflow_result_conflict',409,'该轮已保存不同结果，未重复提交。')
                message_id=row.final_message_id
            else:
                message=Message(conversation_id=turn.conversation_id,role='assistant',content=final['answer'])
                session.add(message);session.flush();message_id=message.id
                gate=final['gate'] or {}; pool_id=None
                if gate.get('allowed') is False:
                    user=session.get(Message,turn.user_message_id)
                    pool=LowConfidenceQuestion(conversation_id=turn.conversation_id,raw_question=user.content,
                                               source='retrieval_low_conf',reason=gate.get('reason') or '检索证据不足')
                    session.add(pool);session.flush();pool_id=pool.id
                self._create_actions(session,row,message_id,actions)
                row.final_message_id=message_id;row.status=final['status']
                row.payload={**row.payload,'final_hash':final_hash,'final':final,'low_confidence_question_id':pool_id,
                             'usage':state.get('agent',{}).get('usage'), 'trace':state.get('trace',[]),
                             'intent':state.get('intent'),'route':state.get('route')}
            saved=session.scalars(select(WorkflowAction).where(WorkflowAction.turn_id==row.turn_id).order_by(WorkflowAction.created_at,WorkflowAction.action_id)).all()
            return FinalizedTurn(row.turn_id,message_id,'wf_'+row.turn_id if final['evidence'] else None,
                                 tuple(action_view(a) for a in saved))

    @database_errors
    def mark_terminal(self,turn,status,code=None):
        if status not in ('failed','interrupted'): raise ValueError('invalid terminal state')
        with self.database.sessions.begin() as session:
            row=self._turn(session,turn,None)
            if row.final_message_id is None:
                row.status=status;row.payload={**row.payload,'error_code':code}

    @database_errors
    def list_actions(self,cid,user_id,message_id):
        with self.database.sessions() as session:
            ChatRepository._owned(session,cid,user_id)
            message=session.get(Message,message_id)
            if message is None or message.conversation_id!=cid or message.role!='assistant':
                raise ServiceError('workflow_message_not_found',404,'回复不存在。')
            actions=session.scalars(select(WorkflowAction).join(WorkflowTurn).where(
                WorkflowTurn.conversation_id==cid,WorkflowAction.origin_assistant_message_id==message_id)).all()
            return tuple(action_view(a) for a in actions)

    @database_errors
    def load_citations(self,cid,user_id,evidence_id):
        with self.database.sessions() as session:
            ChatRepository._owned(session,cid,user_id)
            row=session.get(WorkflowTurn,evidence_id.removeprefix('wf_'))
            if row is None or row.conversation_id!=cid or row.final_message_id is None:
                raise ServiceError('citation_not_found',404,'未找到工作流引用。')
            try:
                final=row.payload['final']
                if digest(final)!=row.payload['final_hash']: raise ValueError('snapshot hash mismatch')
                citations=tuple(CitationRecord.model_validate(c) for c in final['evidence']['citations'])
                if not 1<=len(citations)<=10 or [c.number for c in citations]!=list(range(1,len(citations)+1)):
                    raise ValueError('invalid citations')
                return citations
            except (KeyError,TypeError,ValueError,ValidationError):
                raise ServiceError('citation_invalid',409,'工作流引用快照不完整，请核查。') from None

    def _action(self,session,cid,user_id,action_id,*,lock=False):
        ChatRepository._owned(session,cid,user_id,lock=lock)
        statement=select(WorkflowAction).join(WorkflowTurn).where(WorkflowAction.action_id==action_id,
            WorkflowTurn.conversation_id==cid,WorkflowAction.kind=='ticket')
        row=session.scalar(statement.with_for_update() if lock else statement)
        if row is None:raise ServiceError('workflow_action_not_found',404,'未找到该会话的工单建议。')
        return row

    @database_errors
    def read_action(self,cid,user_id,action_id):
        with self.database.sessions() as session:
            row=self._action(session,cid,user_id,action_id)
            return TicketActionResult(row.action_id,row.status,row.result)

    @database_errors
    def claim_action(self,cid,user_id,action_id,description,ticket_type):
        request_hash=digest({'description':description,'ticket_type':ticket_type})
        with self.database.sessions.begin() as session:
            row=self._action(session,cid,user_id,action_id,lock=True)
            if row.request_hash is not None and row.request_hash!=request_hash:
                raise ServiceError('workflow_action_conflict',409,'该动作已确认其他内容，请先核查原工单。')
            is_new=row.status=='offered'
            if is_new:
                arguments={'description':description,'ticket_type':ticket_type}
                wire={'id':row.tool_call_id,'type':'function','source':'user_action',
                    'origin_assistant_message_id':str(row.origin_assistant_message_id),
                    'function':{'name':'create_ticket','arguments':json.dumps(arguments,ensure_ascii=False)}}
                message=Message(conversation_id=cid,role='assistant',content=None,tool_calls=[wire])
                session.add(message);session.flush()
                row.request_hash=request_hash;row.status='executing';row.request_message_id=message.id
            return {**action_view(row),'tool_call_id':row.tool_call_id,'request_hash':row.request_hash,'is_new':is_new}

    @database_errors
    def settle_action(self,cid,user_id,action_id,outcome):
        with self.database.sessions.begin() as session:
            row=self._action(session,cid,user_id,action_id,lock=True)
            if row.status!='completed':
                row.status='completed' if outcome.payload['ok'] else 'unknown'
                row.result=outcome.payload.get('data') if outcome.payload['ok'] else None
            return TicketActionResult(row.action_id,row.status,row.result)

    @database_errors
    def reconcile_action(self,cid,user_id,action_id):
        with self.database.sessions.begin() as session:
            row=self._action(session,cid,user_id,action_id,lock=True)
            if row.status not in ('offered','completed'):
                receipt=session.scalar(select(Message).where(Message.conversation_id==cid,Message.role=='tool',
                    Message.tool_call_id.collate('utf8mb4_0900_bin')==row.tool_call_id))
                payload=json.loads(receipt.content) if receipt else {}
                if payload.get('ok') is True:row.status='completed';row.result=payload['data']
                else:row.status='unknown'
            return TicketActionResult(row.action_id,row.status,row.result)
