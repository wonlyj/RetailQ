from contextlib import asynccontextmanager
from hashlib import sha256
import asyncio
import json
from pathlib import Path
import time

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import inspect
from retailq.errors import ServiceError
from retailq.workflow.agent import AgentSteps
from retailq.workflow.classification import IntentClassifier
from retailq.workflow.contracts import TurnRef
from retailq.workflow.evidence import EvidenceSteps
from retailq.workflow.graph import build_graph,emit
from retailq.workflow.sql import write_sql


class WorkflowRuntime:
    @classmethod
    @asynccontextmanager
    async def open(cls,settings,repository,chat_repository,gateway,knowledge,policy,coordinator):
        instance=cls()
        instance.settings,instance.repository,instance.chat_repository=settings,repository,chat_repository
        instance.gateway,instance.policy,instance.coordinator=gateway,policy,coordinator
        path=Path(settings.workflow_checkpoint_path)
        if not path.is_absolute():path=Path(__file__).resolve().parents[2]/path
        path=path.resolve()
        identity=[coordinator.identity[k] for k in ('mysql_host','mysql_port','mysql_database')]
        instance.identity=sha256(json.dumps(identity).encode()).hexdigest()
        def check_schema():
            with repository.database.engine.connect() as connection:
                for name in ('workflow_turns','workflow_actions'):
                    if not inspect(connection).has_table(name):
                        raise ServiceError('workflow_schema_missing',503,'请先执行第五章显式数据库迁移。')
        await asyncio.to_thread(check_schema)
        lease=None
        deadline=time.monotonic()+5
        while lease is None:
            lease=coordinator.try_acquire('audit','workflow-init-'+sha256(str(path).encode()).hexdigest())
            if lease is None:
                if time.monotonic()>=deadline:raise ServiceError('workflow_checkpoint_busy',503,'检查点文件正在初始化。')
                await asyncio.sleep(.025)
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            binding=path.with_suffix(path.suffix+'.binding.json')
            if binding.exists():
                if json.loads(binding.read_text(encoding='utf-8'))!={'database':instance.identity}:
                    raise ServiceError('workflow_checkpoint_identity',409,'检查点文件已绑定其他业务库。')
            else:binding.write_text(json.dumps({'database':instance.identity}),encoding='utf-8')
        except (OSError,ValueError):
            lease.close()
            raise ServiceError('workflow_checkpoint_unavailable',503,'检查点路径不可写或绑定记录损坏。') from None
        except BaseException:
            lease.close()
            raise
        try:
            async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
                instance.saver=saver
                # Public read initializes the saver through its supported lifecycle.
                await saver.aget_tuple({'configurable':{'thread_id':'__startup__'}})
                lease.close();lease=None
                yield instance
        finally:
            if lease is not None:lease.close()

    def config_for(self,cid):
        return {'configurable':{'thread_id':self.identity+':'+str(cid)},'recursion_limit':128}

    def graph_for(self,turn):
        turn.evidence=EvidenceSteps(self.chat_repository.rag_retrieve,self.policy,self.settings,deadline=turn.deadline)
        agent=AgentSteps(self.gateway,turn.registry,self.settings,emit,repository=self.repository,
                         turn=turn.ref,deadline=turn.deadline)
        turn.agent=agent
        return build_graph(agent,IntentClassifier(self.gateway,self.settings),turn.evidence,self.repository,self.saver)

    async def rebase(self,turn,projection):
        # graph is request-local but its fixed topology/thread identity is stable.
        graph=turn.graph
        config=self.config_for(turn.ref.conversation_id)
        old=await graph.aget_state(config)
        if old.values and old.next:
            previous=old.values
            if previous.get('turn_id') and previous['turn_id']!=turn.ref.turn_id:
                ref=TurnRef(previous['turn_id'],previous['conversation_id'],previous['user_message_id'])
                await write_sql(self.repository.mark_terminal,ref,'interrupted','previous_run_incomplete')
            await graph.aupdate_state(config,{'status':'interrupted','actions':[]},as_node='log')
            if (await graph.aget_state(config)).next:
                raise ServiceError('workflow_recovery_required',409,'旧轮次尚未安全结束，请核查。')
        return {'turn_id':turn.ref.turn_id,'user_message_id':turn.ref.user_message_id,
            'conversation_id':turn.ref.conversation_id,'user_id':turn.user_id,'raw_question':turn.raw_question,
            'resolved_question':turn.raw_question,'messages':turn.prepared.messages,'intent':'','route':'',
            'evidence':{},'gate':None,'agent':{},'answer':'','finish_reason':None,'actions':[],
            'final_message_id':None,'trace':[],'status':'active'}
