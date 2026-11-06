# 第五章 Workflow 与主力 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans（Native，沿用 RetailQ 已选方式）按任务实施。用户本轮仅批准设计并要求制定、评审计划；本计划待用户审阅后执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原生聊天页交付强制路由/检索/前置闸、可控多步 Agent、SQLite 图状态持久化及独立用户动作。

**Architecture:** 先用现有网关/工具手写裸循环，再将同一决策和执行步骤接入 LangGraph StateGraph。图中 State 传递运行状态并由 AsyncSqliteSaver 自动持久化；MySQL 保存消息、证据、低置信记录与用户动作事实。按钮只发送建议，建工单必须经独立确认 API 才调用原工具。

**Tech Stack:** Python 3.12、现有 FastAPI 0.159.1 / SQLAlchemy 2.1.6 / langchain-core 1.6.13 / langchain-openai 1.7.2，新增兼容 LangGraph、langgraph-checkpoint-sqlite；原 MySQL、Milvus Standalone/BGE-M3/BGE reranker、原生 HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-11-05-ch05-workflow-agent-design.md`（2026-11-05 用户批准）。

## Global Constraints

- 顺序固定：“先不用框架,手写一遍最裸的 Agent 循环”，再用 LangGraph 重构；不使用预制 Agent 跳过热身。
- “闲聊仍调用一次意图分类模型，分类后返回固定话术，不调用主力 Agent”；不得加入关键词预分类。
- 七类固定映射：商品咨询/退款退货 → knowledge；物流/订单/售后 → business；投诉 → complaint；闲聊 → smalltalk。
- 指代消解原样透传；意图识别简单 Prompt/严格 JSON；不做正式版、上下文策略升级、MCP、飞轮入库。
- “create_ticket 不在 Agent 可执行工具白名单内”；Agent 仅可执行原 query_order/query_product/query_logistics，不暴露 query_faq。
- 知识类强制 RAG → 现有 DynamicConfidencePolicy 分数门槛 → Agent；低分兜底/入池，业务类不走闸。
- AsyncSqliteSaver 独立文件 `artifacts/workflow/checkpoints.sqlite`，不得自行改用其他 saver；保持单主机边界，不声称生产负载就绪。
- 每轮最多4次工具执行、6次主力模型调用（含最终流）、整体120秒；计划默认全轮 chat-model token 预算65536，分类输出上限256，最终输出沿用 MAX_OUTPUT_TOKENS=1024；均可配置。
- 最终回答为真实上游流；结构化动作来自无工具申请的决策 JSON `{"actions":[]}`，允许 handoff/ticket，零/一/两项、无重复；不是正文。
- “已转接人工客服”及“您好,我是客服小猫,请问有什么可以帮您的”仅前端模拟；建工单只创建待处理票据，去掉会话“已转人工”副作用。
- State.messages 每轮按 MySQL 已提交事实覆盖投影，无追加 reducer；不重放中断流、不承诺跨库 exactly-once。
- 可测代码 TDD；纯 Prompt/标注数据用冻结样例评估替代；模拟协议、真实模型、真实数据库、浏览器证据分别报告。
- 本地 main、Native 实施；任务完成立即补四项记录并本地提交；不创建 PR、不擅自推送、不输出密钥、不删除数据卷、不覆盖第4章校准产物。

## Review Focus

1. 继续聊天后点击旧气泡建工单：仍绑定旧来源/确认描述，下一轮有工单事实，无错误归属（T3/T6）。
2. MySQL 已提交而 SQLite checkpoint 失败：无 done、无重复入池/消息/工单；下一轮不自动重放旧任务（T3/T5）。
3. 无模型 usage、归一查询失败或超长工具结果：先预留预算、标明估算，不能绕开总量/每请求/时间上限（T1/T4/T5）。
4. 流中断、引用越界、后续 actions 帧缺失：不把部分正文/动作当成可执行成功回复（T4/T5/T7）。
5. 知识关闭、跨 worker、换会话时确认/重复点击：归属和互斥仍有效，只有目标会话能建一次票据（T5/T6/T7）。

## 代码边界与共享契约

新建 `retailq/workflow/`：`contracts.py`（类型/严格解析）、`budget.py`（预算账本）、`agent.py`（裸循环及单步）、`classification.py`（分类适配）、`evidence.py`（前置检索/闸/引用）、`models.py` + `repository.py`（MySQL 运行/动作）、`graph.py`（图装配）、`runtime.py`（saver 生命周期/恢复）、`service.py`（现有聊天服务适配）、`actions.py`（确认动作）。模块不承担彼此职责，不移动现有知识工作台代码。

以下为固定跨任务契约，均在 T1/T3 定义；实现者不得在下游自创同义接口：

- `WorkflowState(TypedDict)`：turn_id/user_message_id/conversation_id/user_id/raw_question/resolved_question/messages/intent/route/evidence/gate/agent/answer/finish_reason/actions/final_message_id/trace/status。消息使用 LangChain BaseMessage，其余为 JSON 可序列化值；messages 无 reducer。agent 内含 step/model_calls/call_ids/seen_calls/pending_selection/usage；pending_selection保存可序列化模型工具申请，不存客户端/Session/锁/单调时钟对象。
- `ChatEvent(event: str, data: dict)` 继续复用现有服务事件；`Emit = Callable[[ChatEvent], None]`；`AgentDecision(selection: AIMessage, actions: tuple[str,...], charge: UsageCharge)`；`UsageCharge(input_tokens:int, output_tokens:int, source:Literal['provider','estimate','reserved'])`。
- `OpenAIModelGateway.decide_agent(messages, tools, *, max_tokens:int) -> AgentDecision`；`classify_intent(messages, *, max_tokens:int) -> tuple[str, UsageCharge]`；`stream_answer(messages, *, max_tokens:int) -> AsyncIterator[ModelChunk]`。保留原 select_tool/stream/extract 兼容接口；ModelChunk 新增可选 usage，默认 None。
- `AgentSteps.decide(state:WorkflowState) -> dict`、`.execute(state) -> dict`、`.answer(state) -> dict` 为 async，返回 State patch，通过构造时注入的 emit 发送 tool_status/token；不依赖 LangGraph。`run_bare_agent(state, steps) -> WorkflowState` 驱动这些步骤，停止时不再请求模型。
- `Reservation(reservation_id:str, stage:str, input_estimate:int, output_cap:int)` 定义于budget.py；`BudgetLedger.reserve(input_estimate:int, output_cap:int, *, stage:str) -> Reservation`，`.settle(reservation:Reservation, charge:UsageCharge|None) -> None`；`.remaining_output(input_estimate:int, desired:int) -> int`；账本导入/导出 state.agent.usage 字典。用完返回受控停止，非无限重试。
- `TurnRef(turn_id:str, conversation_id:int, user_message_id:int)`；`FinalizedTurn(turn_id:str, message_id:int, evidence_id:str|None, actions:tuple[dict,...])`；`TicketActionResult(action_id:str, status:str, ticket:dict|None)`，定义在 contracts.py。

MySQL 计划新增两张**运行元数据表**，不是新业务工具或订单/物流表：`workflow_turns`（turn_id、conversation_id、user_message_id UNIQUE、final_message_id UNIQUE nullable、status、payload JSON、created_at/updated_at）；`workflow_actions`（action_id、turn_id、origin_assistant_message_id、kind、status、request_hash nullable、tool_call_id UNIQUE、request_message_id nullable、result JSON nullable、时间戳；UNIQUE(turn_id,kind)）。主键/工具 ID 用 ASCII 大小写精确比较；外键使用既有 unsigned ID 类型。证据/低置信池 ID/工具消息 ID/usage 放 turns.payload，避免给旧表塞伪工具记录；显式 DDL 及校验迁移，不在请求中建表。

## 文档及环境依据

已用 Context7 解析并查询：FastAPI `/websites/fastapi_tiangolo` 的 [SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/)、[参考](https://fastapi.tiangolo.com/reference/sse/)；SQLAlchemy `/sqlalchemy/sqlalchemy/rel_2_1_6` 的 [事务](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/orm/session_api.rst)、[唯一约束](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/core/constraints.rst)；LangChain `/websites/langchain_oss_python_langchain` 的 [工具调用](https://docs.langchain.com/oss/python/langchain/models)、[usage](https://docs.langchain.com/oss/python/langchain/messages)；LangGraph 的 [流](https://docs.langchain.com/oss/python/langgraph/streaming)、[checkpoint](https://docs.langchain.com/oss/python/langgraph/checkpointers)、[公开状态更新](https://docs.langchain.com/oss/python/langgraph/use-time-travel)。当前 .venv 版本与锁文件一致；已读取实际 ChatOpenAI.bind_tools/astream 签名。

LangGraph 尚未安装，流版本和 saver 真实签名在 T5 先核对，不凭最新网页直接套未安装 API。现有第4章 QueryGateway.normalize 固定最多一次模型调用、输出上限256，当前不提供 usage：T4 在其外部预留保守预算，不改写检索/校准身份文件。Docker 命令不在当前 PATH，文档后备 docker.exe 路径存在但沙箱拒绝启动；只表示本轮未查明运行状态，T8 再核查权限与隔离服务，不视为 Docker 未安装。

---

### T1：裸 Agent 循环、模型协议与预算

**Files:** Create `retailq/workflow/{__init__,contracts,budget,agent}.py`, `scripts/demo_agent_loop.py`, `tests/test_workflow_agent.py`, `tests/test_workflow_budget.py`, `tests/support/workflow_upstream.py`；Modify `retailq/gateway.py`, `retailq/config.py`, `.env.example`, `tests/conftest.py`。

**Interfaces:** 消费现有 ToolRegistry/build_registry、ModelGateway、context.estimate_tokens；产出上文 AgentDecision/UsageCharge/AgentSteps/BudgetLedger/run_bare_agent 和 decide_agent/stream_answer。只读工具白名单在执行器再次检查，不只从模型 schema 隐藏。

所有命令在仓库根目录PowerShell执行，先设 `$ProjectPython = '.\.venv\Scripts\python.exe'`。测试可控上游必须依据收到的上一条工具回执选择下一步，不按请求序号无条件吐预设结果。T1定义测试fixture `controlled_run(scenario:str)->dict`，返回实际tool_names/model_calls/token_events/finish_reason等观测值；后续测试各自扩展受控依赖，不把fixture结果当真实模型评估。

- [x] 写 `test_bare_agent_two_tools_then_real_stream`：可控上游依次 query_order(1001) → 读回执 → query_logistics(1001) → actions=[] → 最终流；断言2次工具执行、4次主力模型请求、成对调用 ID、token 增量来自最终流，且入口未导入 LangGraph。补零/一次工具、缺参收敛及拒绝并行/create_ticket/query_faq/重复ID/重复同参循环/DSML/无效JSON测试。

```python
async def test_bare_agent_two_tools_then_real_stream(controlled_run):
    actual = await controlled_run('order_then_logistics')
    assert actual['tool_names'] == ['query_order', 'query_logistics']
    assert actual['model_calls'] == 4 and actual['token_events'] > 1
```
- [x] 写预算失败测试：预留不足时上游请求计数不增长；usage 缺失保持保守估算；工具超大结果触发预算停止不丢本轮工具配对；总工具4次/模型6次/120秒上限，配置用尽不再调用最终模型。分类/检索有单独账目，不占6次 Agent 上限，但占全轮总量和时间。
- [x] 运行 `& $ProjectPython -m pytest tests/test_workflow_agent.py tests/test_workflow_budget.py -q`，确认因新功能缺失为红；不得把无关环境错误算 TDD 红。
- [x] 实现精确契约及配置 `WORKFLOW_MAX_TOOL_STEPS=4`、`WORKFLOW_MAX_MODEL_CALLS=6`、`WORKFLOW_TURN_TIMEOUT_SECONDS=120`、`WORKFLOW_MAX_TOTAL_TOKENS=65536`、`WORKFLOW_CHECKPOINT_PATH=artifacts/workflow/checkpoints.sqlite`。模型调用前预留 input+output，上游 usage 缺失或请求失败不退还未知消耗；reserve 与实际 usage 不能重复计费。保留现有输入窗口/完整轮裁剪、只读重试和脱敏错误；deadline 由调用层注入，checkpoint 只存已耗/剩余时长。
- [x] 运行上述测试及 `tests/test_gateway.py tests/test_tool_gateway.py tests/test_tool_context.py`；用 `& $ProjectPython -m scripts.demo_agent_loop --mode controlled --question '先查订单1001状态，再查物流位置'` 展示请求/工具/回灌顺序，退出0且执行两工具；`--mode real` 使用现有配置、明确演示工具边界，不创建工单。
- [x] 立即追记 dev-notes 四项和红绿/演示证据，再限定文件本地提交 `feat: add bounded bare agent loop`。

### T2：七类意图、固定路由与标注样例

**Files:** Create `retailq/workflow/classification.py`, `retailq/prompts/{workflow_intent,workflow_agent}.txt`, `evals/ch05_intents.jsonl`, `evals/ch05_workflow_cases.jsonl`, `evals/ch05_annotation.md`, `tests/test_workflow_classification.py`；Modify `retailq/gateway.py`。

**Interfaces:** `resolve_reference(question:str)->str` 原样返回；`route_intent(intent:str)->str` 固定映射；`IntentClassifier.classify(state:WorkflowState)->dict` async 调用 classify_intent 并计入账本；分类网关输出只含 `{"intent":"物流"}` 等严格对象，无工具、无未知键。

- [x] 写 parser/route 可测逻辑的红测试：七类逐一断言出口；未知枚举/重复键/代码围栏/截断/工具输出/非stop拒绝，不能默认送 Agent；`resolve_reference('它到了吗') == '它到了吗'`。同义词分类本身不写假单测。

```python
def test_fixed_routes_and_passthrough():
    assert route_intent('退款退货') == 'knowledge'
    assert route_intent('物流') == 'business'
    assert resolve_reference('它到了吗') == '它到了吗'
```
- [x] 运行 `& $ProjectPython -m pytest tests/test_workflow_classification.py -q` 确认红，实现严格解析、固定映射和一次分类调用；分类输出256 token 上限，Prompt/历史输入先计预算，固定话术不得再调用模型。
- [x] 编写并人工核对21条意图标注（七类各3条，含情绪、缺订单号、退款政策与维修售后区分、恶意指令）；另12条端到端样例覆盖五项用户验收、弱证据、退款后查订单、缺参、Agent建议、缺失政策事实、注入、工单确认。记录来源/预期节点/允许工具/禁止副作用，T8运行前冻结SHA-256，不用这些样例拟合新检索门槛。
- [x] 运行逻辑绿测试；Prompt/数据阶段的验证是逐项标注核对和 T8 真实模型评估，尚未真实运行不得写“分类质量通过”。最终回答 Prompt 注入独立证据/工具数据、严格 actions 控制示例、演示数据标识和缺失事实拒答约束。
- [x] 立即留痕并提交 `feat: add seven-intent workflow routing`。

### T3：MySQL 运行记录、证据与动作持久化

**Files:** Create `retailq/workflow/{models,repository}.py`, `db/init/05-workflow.sql`, `scripts/migrate_workflow.py`, `tests/test_workflow_repository.py`, `tests/test_workflow_schema.py`；Modify `tests/support/database.py`, `tests/conftest.py`, `retailq/context.py`。

**Interfaces:** `WorkflowRepository.start_turn(body:ChatRequest)->TurnRef` 同事务创建/校验会话、导入历史、写user及turn；`.load_projection(cid:int,user_id:str,*,before_message_id:int|None=None)->list[list[BaseMessage]]`；`.record_tool_request(turn:TurnRef,selection:AIMessage)->int`；`.record_tool_result(turn:TurnRef,call_id:str,payload:dict)->int`；`.finalize(turn:TurnRef,state:WorkflowState)->FinalizedTurn`；`.mark_terminal(turn:TurnRef,status:str,code:str|None)->None`；`.load_citations(cid:int,user_id:str,evidence_id:str)->tuple[CitationRecord,...]`；`.list_actions(cid:int,user_id:str,message_id:int)->tuple[dict,...]`。所有同步短事务，async调用在线程内执行。

- [x] 写真实隔离 MySQL 红测试：finalize 重入同一turn仅1条助手/低置信记录/每类动作；不同最终载荷冲突报409；finalize中途失败整体回滚；大小写不同 call ID 不合并，重复同ID仅一回执；跨用户/跨会话拒绝。新 schema 先由独立显式迁移测试创建，不以 ORM create_all 代替DDL验证。

```python
def test_finalize_same_turn_is_idempotent(repo, turn, low_conf_state):
    first = repo.finalize(turn, low_conf_state)
    assert repo.finalize(turn, low_conf_state).message_id == first.message_id
    # 用实际SQL断言该turn只有1条最终助手消息、1条关联低置信记录。
```
- [x] 运行 `& $ProjectPython -m pytest tests/test_workflow_schema.py tests/test_workflow_repository.py -q` 确认红；若数据库不可达先恢复隔离环境，不绕过集成测试。
- [x] 实现两张表、DDL摘要/字段/索引/外键校验及 `python -m scripts.migrate_workflow --scope test|demo`（只允许本机3307/retailq_test 或3306/retailq；首次不支持任意目标，其他环境需独立显式迁移方案）。幂等检查不改旧表结构、不清数据。新表主键为字符串，不能直接套用旧knowledge迁移器“主键id自增”的verify；独立验证实际主键/非自增类型。测试清理先workflow_actions/turns再旧消息表，保留已有测试库防误删护栏；schema漂移不静默ALTER。
- [x] 实现 finalize 原子保存正文、证据快照、动作建议及可选低置信池ID；turn状态为 completed/limited，错误为 failed/interrupted。相同turn+相同结果重入返回原ID；低置信池不新增列。证据 ID `wf_<turn_id>` 仅用于工作流来源，不塞进旧 tool_call_id。record_tool_request/result 在turn行锁下按call ID记录消息ID，可重入且内容不一致拒绝。
- [x] start_turn后加载历史必须传 `before_message_id=turn.user_message_id`，再追加当前用户一次；写测试断言模型输入当前问题仅出现一次。mark_terminal不得覆盖已有completed/limited及final_message_id，checkpoint落后仍保留SQL已提交终态。
- [x] 写旧按钮来源投影测试：另一个自然语言轮后写 user_action 申请/回执，投影只有一次确定性工单事实、保留 origin_assistant_message_id，不伪造模型调用；失败/unknown写成“结果尚未确认”而非成功。load_projection 再交现有完整轮预算裁剪。
- [x] 运行本任务测试及 `tests/test_context.py tests/test_tool_context.py tests/test_repositories.py tests/test_rag_repository.py`；逐阶段留痕并提交 `feat: persist workflow turns evidence and actions`。

### T4：强制检索、前置闸与引用契约

**Files:** Create `retailq/workflow/evidence.py`, `tests/test_workflow_evidence.py`, `tests/test_workflow_sources.py`；Modify `retailq/api/sources.py`（新增工作流来源路由，保留旧路由）。

**Interfaces:** `EvidenceSteps.retrieve(state:WorkflowState)->dict`、`.gate(state)->dict` async；`validate_answer_references(answer:str,citations:tuple[CitationRecord,...])->None`；来源 API `GET /api/conversations/{cid}/workflow-turns/{turn_id}/sources[/{number}/document]`，复用 public_citation/load_source_document 和归属校验。

- [x] 写红测试：知识出口调用顺序 retrieve→gate→Agent；退款证据先于订单工具；业务/投诉/闲聊零检索。低分/无候选只兜底且入池原问题、Agent计数0；门槛配置缺失/身份失配/服务异常为 error、不入低置信池。来源证据全集连续编号，正文可仅引用子集；越界/缺必需引用不得成功终结。

```python
def test_answer_rejects_out_of_bundle_number(citations):
    with pytest.raises(ServiceError):
        validate_answer_references('政策见[99]。', citations)
```
- [x] 运行 `& $ProjectPython -m pytest tests/test_workflow_evidence.py tests/test_workflow_sources.py -q` 确认红，实现原 `retrieve_knowledge(question)` + DynamicConfidencePolicy；通过后才 prepare_bundle 并纳入 Agent 完整输入预算，不用旧 RAGDecision 生成回答。保持检索/重排/归一/策略身份逻辑不变，不自动调阈值。
- [x] 调检索前向 BudgetLedger 预留现有 query_normalize Prompt+原问题的保守输入估算及256输出；现有同步检索无usage回传，记 reserved（不假称实耗），每次最多一次归一调用。预留不足就不启动检索；embedding/rerank另记调用阶段，不伪造其token计费。线程请求超时/取消仍须保留会话租约到后台任务结束，不能让新轮与仍运行检索共享未释放资源。
- [x] 实现新来源路由和不可变快照验证；回答流结束只做引用结构检查，失败不保存完整答复、不发sources/done，已流出的文本为未完成。知识类完全缺信息的追问或停止用确定性文本，不伪造带证据完整答案；不新增生成后置信检查。
- [x] 运行本任务测试及 `tests/test_sources_api.py tests/test_rag_answer.py tests/test_rag_configuration.py`，覆盖归档丢失/篡改、跨用户读取；留痕并提交 `feat: gate mandatory retrieval before agent streaming`。

### T5：LangGraph 骨架、AsyncSqliteSaver 与主入口替换

**Files:** Create `retailq/workflow/{graph,runtime,service}.py`, `tests/test_workflow_graph.py`, `tests/test_workflow_recovery.py`, `tests/test_workflow_chat.py`, `tests/support/workflow_worker.py`；Modify `retailq/app.py`, `retailq/api/chat.py`, `retailq/config.py`, `requirements.in`, `requirements.lock`, `requirements-dev.lock`, `scripts/client.py`, `tests/support/database.py`（每个API测试独立checkpoint路径）；更新 `tests/test_chat_api.py` / `tests/test_tool_chat.py` / `tests/test_rag_chat.py` 和 `tests/support/{upstream,tool_upstream}.py` 的已改变入口契约，保留未变故障断言。

**Interfaces:** `build_graph(agent:AgentSteps,classifier:IntentClassifier,evidence:EvidenceSteps,repository:WorkflowRepository,checkpointer:AsyncSqliteSaver)->CompiledStateGraph`；`WorkflowRuntime.open(settings:Settings,repository:WorkflowRepository,chat_repository:ChatRepository,gateway:OpenAIModelGateway,knowledge:KnowledgeRuntime|None,policy:DynamicConfidencePolicy|None,coordinator:Coordinator)` 是async context manager。runtime提供 `.graph_for(turn:PreparedWorkflowTurn)->CompiledStateGraph` / `.rebase(turn:TurnRef,projection:list[list[BaseMessage]])->WorkflowState` async / `.config_for(cid:int)->dict`。`WorkflowChatService.prepare(body:ChatRequest)->PreparedWorkflowTurn`、`.stream(turn:PreparedWorkflowTurn)->AsyncIterator[ChatEvent]`、`.release(turn:PreparedWorkflowTurn)->None`、`.aclose()->None`兼容API生命周期。PreparedWorkflowTurn包含TurnRef、user_id、prepared、registry、lease/released；每个请求单独创建agent/registry/emit绑定并compile到共享saver，不将可变请求对象存到全局steps，避免跨会话串流。graph_for生成的相同拓扑使用同一thread_id恢复。

- [x] 先按Context7核对目标LangGraph与saver版本/API及Python3.12兼容性，用 pip dry-run 对现有锁版本约束解析新增依赖；仅新增依赖及其传递依赖，若必须升级已有固定栈先停问用户。解析版本写进记录后，锁文件安装、pip check；独立环境按锁安装验证。核对 astream/custom/durability/AsyncSqliteSaver真实签名，不默认依赖version=v2输出格式。
- [x] 写红测试：graph节点/固定路由覆盖七类；AsyncSqliteSaver关闭重开同一文件仍读到State；API主入口默认且唯一走新图，知识关闭也有跨服务会话互斥；旧会话初始化、归属不匹配404、同会话409、不同会话可独立运行；缺工作流表/不可写SQLite启动明确不可用、不退回旧ChatService。

```python
async def test_restart_preserves_state_and_clears_no_history(restarted_graph, config):
    snapshot = await restarted_graph.aget_state(config)
    assert snapshot.values['raw_question'] == '订单1001的物流到哪了'
    assert snapshot.next == ()
```
- [x] 运行 `& $ProjectPython -m pytest tests/test_workflow_graph.py tests/test_workflow_recovery.py tests/test_workflow_chat.py -q` 确认红；实现 spec 图，graph 节点使用 T1单步/T2分类/T4检索，最终log节点调T3 finalize；compile时注入saver，astream使用custom或custom+updates与 `durability='sync'`，把业务事件映射SSE，不把checkpoint内部数据发前端。
- [x] 实现 lifespan打开/关闭saver与图资源；构造会话锁不依赖knowledge是否启用，沿用Coordinator同主机数据库作用域。thread_id由规范化数据库身份+cid生成；同一SQLite文件不可意外绑定不同数据库身份，单独绑定元数据防串环境。运行时对象由闭包注入，State只含可序列化数据；超时不依赖跨重启单调时间比较。
- [x] rebase持锁先核对MySQL终态与checkpoint：未完成旧turn标interrupted，禁止 `ainvoke(None)`/自动续跑。若snapshot.next仍有任务，用公开 `aupdate_state(...,as_node='log')` 写已中断快照并验证next为空，再带新输入启动START；不调用旧log业务函数、不改私有表。MySQL已完成而checkpoint落后时保留完成事实，清除旧pending但不重复finalize；无法安全对账则报可识别错误，不自动重放。
- [x] 流顺序：meta先出；节点状态/工具状态及上游token过程中发送；graph退出且sync checkpoint成功后，读取已完成turn，再发sources/actions/done。length保留截断提示但无完整答复/动作；checkpoint/log/最终事务失败只error。取消时记终态、关闭生成器，shield仅短写；等待未完成同步任务结束后释放租约。scripts/client.py支持并校验workflow/actions/新sources，旧来源兼容，仍拒绝unknown帧和done后事件。
- [x] 入口旧测试中的固定两次模型调用/固定帧序列更新为分类+Agent+真实流的契约；首字延迟/断开测试使用business问题，不用已固定回复的“你好”测模型流。网关自身协议故障仍独立覆盖，不删除原测试只为过绿；不增加默认关闭新图的兼容开关。
- [x] 写故障红绿：子进程在工具回执后、MySQL finalize后checkpoint前退出；重启继续新问题不重复用户消息/入池/工具/工单，部分正文不恢复为完成。旧会话轮边界覆盖而非追加历史；日志验证节点/route/预算/结束码，无密钥与内部思维链。
- [x] 运行本任务与入口相关回归；留痕、记录依赖实际版本/恢复证据，提交 `feat: orchestrate customer chat with persistent LangGraph`。

### T6：独立工单动作 API 与旧工具解耦

**Files:** Create `retailq/workflow/actions.py`, `retailq/api/actions.py`, `tests/test_workflow_actions.py`；Modify `retailq/workflow/repository.py`, `retailq/api/__init__.py`（仅确需导出时）, `retailq/app.py`, `retailq/repositories.py`, `retailq/tools/business.py`, `tests/test_tools.py`, `tests/test_repositories.py`。

**Interfaces:** `TicketActionService.confirm(cid:int,user_id:str,action_id:str,description:str,ticket_type:str)->TicketActionResult` async；`.get(cid:int,user_id:str,action_id:str)->TicketActionResult` async；`.aclose()->None` async等待本服务持有的后台收尾任务。API `POST /api/conversations/{cid}/actions/{action_id}/ticket` body `{user_id,description,ticket_type}`，`GET`同路径以user_id查询状态；`WorkflowRepository.claim_action(cid:int,user_id:str,action_id:str,description:str,ticket_type:str)->dict` 返回action_id/status/tool_call_id/origin_assistant_message_id/request_hash/result；`.settle_action(cid:int,user_id:str,action_id:str,outcome:ToolOutcome)->TicketActionResult`。方法均短事务并使用同会话锁；claim返回旧终态时禁止再次调用工具。

- [ ] 写真实MySQL红测试：投诉建议本身零tickets；确认只调用现有create_ticket，成功为待处理且conversation.status不改已转人工；handoff无后端写接口。重复同动作同payload返回原票据，改payload冲突409；伪造action、跨用户、无ticket建议404/409；处理中返回busy、不排队；继续聊天不执行动作。

```python
async def test_confirm_twice_reuses_ticket(action_service, ticket_args):
    first = await action_service.confirm(**ticket_args)
    second = await action_service.confirm(**ticket_args)
    assert first.ticket['ticket_no'] == second.ticket['ticket_no']
    # 另以真实SQL断言票据增量为1、会话status仍为进行中。
```
- [ ] 运行 `& $ProjectPython -m pytest tests/test_workflow_actions.py tests/test_tools.py tests/test_repositories.py -q`，确认新增行为为红，实现动作验证/确认与工具复用，删除旧status副作用并改说明；call ID由action稳定派生，<=64，来源user_action+origin消息ID保存到动作/申请元数据，不能由客户端指定工具名/会话身份。
- [ ] claim在锁内冻结description/type/hash，写executing及唯一tool request；工具成功及原tool回执维持同事务；settle更新动作。若进程在票据提交后、settle前退出，GET通过稳定tool回执核对并恢复completed；若executing无确定回执，标unknown、不重新执行。pending/unknown不自动重试；重复请求只查询，前端可GET核对状态。
- [ ] 明确后台租约所有权：工具超时返回pending或POST被取消时，不能在请求finally中释放会话租约。TicketActionService保留registry、lease及后台settle任务的强引用，等待 `registry.drain()`，核对持久回执并更新动作状态，最后释放lease；与现有ChatService.release语义一致。GET可读待确认状态，写对账仍须持有会话锁，不能和后台任务抢同一动作。lifespan先停止接收新动作、等待action_service.aclose完成，再关闭MySQL，不对aclose套用会使数据库先关闭的短路超时；若外部强制终止进程，保留unknown/executing供重启对账，不报告正常关闭、未写入或已取消后台写操作。
- [ ] 写故障/竞态测试：工具写超时仍可能后台成功；确认连接断开/刷新不重POST；跨服务并发同动作仅1票据；最终回执与action状态落差可核对，结果不得错误显示为未创建。旧气泡在新轮后确认仍绑定旧来源，下一轮T3投影正确。
- [ ] 增加受控慢写测试：POST已超时/取消但后台工具仍阻塞时，第二服务的同会话chat/confirm必须409，其他会话仍可继续；释放阻塞后只有1票据、原租约才释放、随后新聊天可继续并看到工单事实；aclose等待的测试先确认数据库尚未关闭，再放行写入并断言有序退出。
- [ ] 运行本任务及工单相关回归，记录实际SQL票据计数与status；留痕并提交 `feat: require explicit independent ticket confirmation`。

### T7：原生聊天页两个按钮与确认交互

**Files:** Modify `retailq/web/{app.js,index.html,styles.css}`；Create `retailq/web/actions.js`（动作UI/状态校验模块）, `tests/support/workflow_frontend_races.mjs`, `tests/test_workflow_frontend.py`。

**Interfaces:** actions.js导出 `renderActions(container,message,context)` 与 `validateActions(payload)`；context固定包含userId/cid/originMessageId及目标session引用。现有app.js维护SSE/消息，交由动作模块渲染； sources支持旧toolCallId或新workflowTurnId，两种来源路径不混用。

- [ ] 写必要的行为红测试：actions只允许完成且有效来源，正文提到“建工单”不能生成按钮；确认取消零请求；handoff确认零HTTP票据请求，精确显示两句指定文案；ticket确认仅1 POST，两按钮任意顺序互不触发；半截SSE/无done不启用动作。测试按行为，不逐条镜像CSS/DOM实现。

```javascript
// workflow_frontend_races.mjs：实际点击并确认后断言
assert.equal(ticketPostCount, 0); // 仅确认“转人工”后
assert.match(chatText, /已转接人工客服/);
assert.match(chatText, /您好,我是客服小猫,请问有什么可以帮您的/);
```
- [ ] 运行 `& $ProjectPython -m pytest tests/test_workflow_frontend.py -q` 确认红（浏览器运行器复用仓库既有方式，不新增前端框架）；实现两独立确认交互、工单description/type输入及提交状态、handoff模拟消息。用户不点则输入照常可用；模拟问候不上传为真实人工/模型历史。
- [ ] ticket未知结果禁重POST，允许GET核查；刷新从服务器动作状态恢复，手改浏览器状态不能冒充已创建。旧气泡动作仍绑旧消息；会话切换时回执只更新原session，按钮disabled仅约束自身提交/会话忙，不把handoff与ticket绑定。服务端动作文本当数据用DOM textContent渲染。
- [ ] 运行本任务测试，再实际浏览器验证投诉双按钮、不选继续、两种顺序、取消、重复点/刷新/超时、换会话、Agent只建议一个、历史引用、停止流；保存截图和请求/票据证据。缺真实浏览器环境时明确未验项，不以单测冒充点击验收。
- [ ] 立即留痕并提交 `feat: add separate handoff and ticket chat actions`。

### T8：冻结评估、完整验证、审查与交付

**Files:** Create `scripts/evaluate_workflow.py`, `scripts/smoke_workflow.py`, `docs/ch05-workflow-agent.md`, `docs/ch05-evaluation.md`, `docs/ch05-manual-acceptance.md`, `tests/test_workflow_eval.py`, `docs/superpowers/reviews/2026-11-05-ch05-code-review.md`；Modify `README.md`, `dev-notes/ch05.md`；运行产物只进 `artifacts/evals/ch05/<新目录>/`。

**Interfaces:** CLI `python -m scripts.evaluate_workflow run --base-url URL --output DIR` 与 `review --report PATH`；`smoke_workflow --base-url URL --output PATH`；evaluate报告记录expected/completed/case IDs、样例/Prompt/代码/依赖/模型/语料/策略身份、实际节点/工具/动作/usage/票据SQL核查和人工判定。退出0仅全部完成且已评通过，1失败，2环境不足/待人工评。

- [ ] 给评估器写红测试：缺例/重复/中断/未评/无真实DB证据不能通过；单纯出现工具名不等于实际执行；绑定实际加载策略SHA而非只记启动参数；run目录已存在拒绝覆盖。实现并绿测 `tests/test_workflow_eval.py`。

```python
def test_review_cannot_pass_unreviewed_case(report_path):
    assert main(['review', '--report', str(report_path)]) == 2
```
- [ ] 冻结T2样例、Prompt和验收规则后真实运行：21条分类需21/21命中标注；12条端到端逐例符合标注；五项用户验收全部通过；必检索/弱证据不进Agent/业务不预检索/不自动建票据/确认单票据等代码约束100%。缺失事实不得编造，退款日期不承诺；人工逐事实核对引用与真实工具回执。失败则保留报告，修Prompt后新目录重跑，不事后改标签制造通过；小样本不代表生产泛化。
- [ ] 在已有隔离MySQL/Milvus及匹配的第4章语料/策略上验证，不在演示库自动迁移/建票据。核对Docker后备CLI权限与服务；缺环境则记录并完成不依赖环境的工作。真实评估串行进行，评估结束再完整回归，避免一边改代码一边把测试结果当最终证据。
- [ ] 执行 `& $ProjectPython -m pytest -q`、`& $ProjectPython -m pip check`；按marker/环境记录跳过原因，仓库已有故障项按其显式隔离库规则补跑。独立锁定环境安装后跑与变更相关测试；第4章真实报告仅作历史证据，不替代本章回归。测试失败用 systematic-debugging，保留首轮失败与修复记录。
- [ ] 使用 requesting-code-review 技能派独立审查者核对固定起点2077c90至最终变更，按 spec/仓库规范检查；阻塞问题修复并复验，再记录 code review 结论。Native依然由主代理实现，不因评审而改变执行方式。
- [ ] 文档给完整PowerShell命令：依赖安装、隔离服务启动、显式05迁移、匹配语料/策略路径、裸循环演示、uvicorn启动、五个验收问题及确认按钮步骤。默认沿用本机 `--workers 2`，SQLite/锁跨worker实测通过才如此推荐，否则不得宣称支持；出现选型矛盾按原要求停问，不暗中退回内存或更换saver。
- [ ] 实际运行所有交付命令，记录启动/工具/节点/票据与恢复证据；更新README旧“每轮一个工具”“工单=已转人工”等过时说明，并保留随机演示/本地身份/SQLite限制。最后按 verification-before-completion、finishing-a-development-branch 执行已授权本地main收尾；finish即时追记四项，本地提交，推送等待用户指示。

## 自审与执行交接

规格覆盖：裸循环→T1；指代/七类→T2；状态/持久事实→T3/T5；强制RAG/前置闸/来源→T4；主入口/日志→T5；独立按钮/工单→T6/T7；标注/评估/完整回归/交付→T8。所有Review Focus均已归属测试任务；T1→T2/T3→T4→T5→T6→T7→T8，Native顺序实施，不并行改共享接口。

计划完成自审后另做独立只读评审，问题就地修订并保留记录。用户审阅计划通过后才实施；每任务复验后即时更新本计划勾选和dev-notes，不能收尾补记。最终交付不是“计划已完成”的代称，功能/真实验证/浏览器未完成项必须分别报告。
