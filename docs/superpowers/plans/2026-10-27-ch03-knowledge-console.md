# RetailQ 知识审核工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可手动挖掘历史对话、人工审核分类后入库、逐来源追踪chunk去向的知识工作台。

**Architecture:** 原文继续保存在既有MySQL表，审核意图、操作、组和确证去向存于版本2本地审计记录；通过后才写knowledge_chunks及Milvus。FastAPI进程内作业协调器复用唯一KnowledgeRuntime，所有对话挖掘入口共用准入门禁；原生网页读取安全的查询投影，以真实阶段轮询，不另开CLI子进程。

**Tech Stack:** 现有Windows / Python3.12 `.venv`，FastAPI0.159.1、SQLAlchemy2.1.6、Pydantic2.14.1、langchain-openai1.7.2、HTTPX0.28.1、PyMilvus3.0.2、Milvus Lite3.2.2；MySQL8.4、硅基流动BAAI/bge-m3、现有聊天模型、原生HTML/CSS/JS，不增依赖。

**Spec:** `docs/superpowers/specs/2026-10-27-ch03-knowledge-console-design.md`，2026-10-27用户“进入实现计划”批准。布局草图用户已确认，文件位于本机忽略目录 `.superpowers/brainstorm/2026-10-27-knowledge-review/content/knowledge-review-layout.html`；没有该临时文件的执行者按spec第4节及本计划页面合同实现，不能把草图样例当业务数据。

**Execution:** 沿用用户已明确选择的Native，由作者逐任务执行、每任务红绿门禁及提交，末尾一次新上下文整体审查。本文待用户审阅，不在制定计划时实施。产品代码起点87a3660，设计文档提交75b4b46。

## Global Constraints

- 先审核，再入库；所有网页/CLI/scheduler对话挖掘都受门禁约束，未审核新知识不写knowledge_chunks或Milvus，准备过程允许内存中的去重向量计算。
- 原始db/init/03-knowledge.sql两表字段/索引不变，不增MySQL持久表或列；MySQL原文、审计文件、向量索引分工不可混淆。原DDL SHA256应保持A49DF681780414612FA726D78BD3BC98FB20A1D54E2C5FA6F0E066710A240CDA。
- BGE固定BAAI/bge-m3、1024维、COSINE，Milvus本地单进程/一个owner。category/questions/answer进向量，其余审核/来源元数据不进向量；不加关键词/混合/重排，不换服务商。
- query_faq的keyword严格字符串1–64及found/items、id/question/answer/category成功合同不变；/health原三字段、其他工具、原聊天SSE不变。
- 同时仅一个未结束版本2审核任务、一个实际维护作业；等待人工时不占SQL事务/维护锁。sealed独立终态优先，保留现存数据并允许新任务，旧命令不能复活。
- 决定/ prepared / applied在同一组原子记录；202之前先持久化queued操作。任何跨资源成功结论必须同时核对成员状态、目标ID和三字段指纹，不能以存在某个文本相近chunk代替确证。
- 只选择已结束、首user/末非空完整assistant来源；一次1–100个不同会话，列表默认20、上限100。不结束真实对话，不改问答、不拆组、不任意换目标、不撤销已发布知识。
- 管理页/API仅loopback客户端与localhost/127.0.0.1/[::1]主机；写操作同源Origin、自定义头、严格JSON；不新增CORS放行，不把浏览器user_id当管理员身份。
- 代码TDD，纯分类Prompt/样例真实标注评估，网页真实浏览器验证；用户已选择自行做云端测试，开发侧不发真实模型请求/代理探测。人工B3仍跳过，新增审核门禁的本地scheduler回归照测。
- 新增运行文件和反馈仅在artifacts/或.superpowers/，不提交.env、密钥、数据库或运行报告。保留原环境/锁版本，不自动安装包。
- 每个任务完成及review/finish当场追记dev-notes/ch03.md四项：用户关键原话、产出/结果、拒绝纠偏、失败返工；不在结尾补写历史。
- 直接main，不建分支/worktree/PR；限定文件提交，不自动推送。设计/计划批准均不等于功能验收或前序章节finish。

## Review Focus

1. **进程在文件/SQL/Milvus不同边界终止、文件fsync/replace失败**：只依据确证恢复同ID，不能重复发布或把prepared展示成已入库；任务1/2/4/10分别覆盖原子文件、SQL回滚、业务恢复和真实kill。
2. **两个标签页、旧revision、重复或复用请求键、先批准后拒绝**：最多一次决定和一次应用，同请求复用原结果，冲突409；任务1/4/6/8/9覆盖持久命令、语义幂等、准入和浏览器。
3. **损坏/永久冲突任务、封存前部分来源已应用、旧队列重启、整组多来源不完整**：可以明确封存释放名额，未确证来源仍可重选，未知去向不猜；任务3/4/5/7/10覆盖来源水位、sealed优先与旧版隔离。
4. **浏览器断连、关闭服务时HTTP尚未结束/后台线程还在写、CLI和scheduler与页面同时进入**：关页不取消已受理命令，真正线程离开后才关资源，所有入口同一门禁；任务6/7/8/12覆盖受控事件和实际浏览器。
5. **大BIGINT、超长/控制字符category、HTML/DSML来源文字、超预算分类目录**：ID不被JS舍入，不执行HTML/模型指令，不凭截断生成分类，不绕过原文预算；任务1/2/3/8/9/11覆盖严格模型、预算、投影及浏览器。

## 文件职责与任务依赖

| 文件 | 职责/任务 |
| --- | --- |
| 新retailq/knowledge/review_contracts.py、review_store.py | 不可变记录、状态/ID规则、原子审计与CAS，任务1 |
| 新retailq/knowledge/review_repository.py | 来源/分类SQL查询、审核组事务应用与确证恢复，任务2 |
| 新retailq/knowledge/review_preparation.py、review_classification.py | 只准备至待审核、分类批次/预算，任务3 |
| 改qa_gateway.py，新prompts/qa_category.txt | 严格分类建议接口，不批准知识，任务3 |
| 新retailq/knowledge/review_application.py；改service.py/repository.py | 决定应用、指定ID向量补齐、封存，任务4 |
| 新retailq/knowledge/review_queries.py | 只读分页投影、去向、旧版与故障可见性，任务5 |
| 新retailq/knowledge/review_jobs.py；改runtime.py/app.py | 准入租约、生命周期作业/关闭，任务6 |
| 改mining.py、scheduler.py、scripts/knowledge.py | 移除旧自动发布路径，统一CLI/调度，任务7 |
| 新retailq/api/knowledge.py、knowledge_schemas.py、knowledge_access.py | 管理路由、严格JSON与本机同源限制，任务8 |
| 新web/knowledge.html、knowledge.css、knowledge-api.js、knowledge.js、knowledge-views.js | 页面壳、请求/轮询、交互状态、纯DOM渲染，任务9 |
| 改web/index.html/app.py | 聊天导航及正式/knowledge入口，任务9 |
| 新tests/support/reviews.py及test_review_*.py | 真实存储/本地模型替身fixtures、各组件行为，任务1起各任务增加 |
| 新tests/support/review_worker.py、scripts/verify_knowledge_review_recovery.py | 隔离测试库真实进程kill与恢复证据，任务10 |
| 新scripts/evaluate_knowledge_categories.py、smoke_knowledge_console.py、evals/ch03_console_categories.jsonl | 分类标注评估与用户真实操作后的只读反馈，任务11 |
| 新tests/support/review_console_server.py；新docs/ch03-knowledge-console.md | 前者任务9建立真实app的本地测试服务，后者任务12记录最终浏览器验收/启动与备份 |

顺序1→2→3→4→5→6→7→8→9→10→11→12。任务3/4独立于旧运行入口实现；任务6接入新引擎，任务7删除所有旧自动发布调用并迁移旧测试语义，避免前置任务引用尚不存在的组件。相关测试按任务增量创建，不提前提交未来任务的失败测试。

## 共享合同与实施规则

这些是构建者已决定的细节，执行者不再自行改名或换类型；内部签名见各任务Interfaces。

- 内部MySQL主键仍int，范围1..2^63-1；**新管理API**中的conversation/staging/chunk及message ID一律规范十进制字符串，不用JSON数值。`9007199254740993`必须原样往返。run/group/operation/request key用32位小写hex；revision为非负安全整数。旧客服工具id仍保持原int合同。
- operation_id直接采用request_key；创建任务的run_id采用该创建操作ID，省去非原子“请求键→操作”索引。相同key对不同规范请求体返回409，相同体复用该命令及原快照。group_id取sha256(run_id + ':' +逗号连接排序staging IDs)前32位，不依赖LLM提供ID。
- 路径：knowledge根目录/reviews/operations/{operation_id}.json；reviews/runs/{run_id}/run.json、groups/{group_id}.json、sealed.json。旧版runs/run-*.json只读。状态写临时同目录文件→flush→fsync→os.replace；失败不把旧文件移走，拒绝路径穿越/链接逃逸；无目录删除来“恢复”。
- 记录采用Pydantic2.14.1严格、extra=forbid的frozen模型，集合用tuple及不可变条目，不能靠frozen包mutable dict声称深只读。读取用model_validate_json；可信转换也必须重新校验更新，不以model_copy(update=...)绕过校验。最终JSON UTF-8，时间为带+08:00的ISO8601字符串。
- RunPhase：queued/extraction/dedup/classification/waiting_review/applying/vectorize/completed/failed/interrupted/conflict/sealed。OperationPhase：queued/running/succeeded/failed/interrupted/sealed。组决定pending(无decision)/approved/rejected；应用prepared/applied/conflict；向量状态从MySQL确认，不自创done。
- 操作kind：prepare/resume/decide/retry_vectors/seal。命令及其来源快照不可变，阶段/错误可以CAS更新。sealed优先于任何run/operation状态，读到损坏不可当空列表继续。
- `Page[T]`：items tuple[T,...]、total、offset、limit。错误复用ServiceError安全码：knowledge_job_busy、knowledge_review_pending、knowledge_review_stale、knowledge_review_conflict、knowledge_review_corrupt、knowledge_review_sealed、knowledge_request_conflict、knowledge_run_version_mismatch、knowledge_legacy_readonly；管理边界403，缺配置503，严格输入422，资源不存在404，冲突409。
- 测试替身只替外部模型/嵌入，MySQL3307与真实Lite负责存储证据；不能从FakeEmbedding结果声称BGE语义质量。新测试helper只能在tests/support/，生产模块不导入tests或加测试分支。

## 执行前检查（计入任务1准备，不另建脚手架任务）

在PowerShell使用现有环境；以下命令开始实施时才运行，不在本计划编写时伪称已通过：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $Docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql
& $ProjectPython -m pytest -q
& $ProjectPython -m pip check
```

Expected：隔离mysql healthy；完整现有测试通过（上一实现提交的历史证据222 passed，执行时取实际结果）；pip无冲突。Docker未运行先启动既有Desktop，失败按systematic-debugging，不删卷/重装环境。停止会占同目录的演示进程仅在确需维护时由用户处理，测试使用不同目录。

Native建立本计划专有 `.superpowers/sdd/2026-10-27-ch03-knowledge-console/`：账本首行指向本计划，逐任务brief/BASE/红绿日志；采用前序已验证的Windows记账方式，不覆盖旧计划工作区。首个产品代码前按执行技能核对跨任务Interfaces；冲突以批准spec为准，具体裁定记Ruling。

---

### Task 1: 严格领域记录与原子审计存储

**Files:** Create `retailq/knowledge/review_contracts.py`、`retailq/knowledge/review_store.py`、`tests/test_review_store.py`、`tests/support/reviews.py`（先只含纯记录工厂）；不改MySQL/运行入口。

**Interfaces:**
- `ReviewStore(root: Path, *, clock: Callable[[],datetime])`在构造时不创建owner或连接资源。所有写操作仅由已经持有runtime维护租约者调用；内部RLock/CAS同时保护本进程读改写，原子replace保护读者。
- 模型：`SourceQA(staging_id,batch_no,source_ref,question,answer,message_ids)`；`ReviewDecision(operation_id,kind:Literal['approved','rejected'],category,reason,decided_at,actor:Literal['本机操作']='本机操作')`；`MemberDisposition(staging_id,status,reason,chunk_id)`；`PreparedApplication(operation_id,chunk_id,fingerprint,members)`；`ApplicationRecord(phase,prepared,error)`；`ReviewGroup(run_id,group_id,revision,members,representative_id,target_chunk_id,target_fingerprint,questions,answer,suggestion,decision,application)`。成员与问法都是tuple，代表/旧目标恰一项，suggestion和decision/application可空。
- `CategorySuggestion(group_id,category: str|None,reason: str,recommend_reject: bool)`；reason≤300；recommend_reject只作建议，不是决定。`SafeFailure(code,message)`不包含raw异常；`AuditFault(run_id,code,message)`代表可见故障；`SealRecord(run_id,operation_id,created_at,reason)`；`BatchRecord(batch_no,sources:tuple[ConversationSnapshot,...])`；`RunCounters(extracted,review_pending,approved,rejected,applied,vectorized,pending_vectors)`均非负整数，applied只计已确认应用的批准组，拒绝单计；vectorized计本run记录过写入意图并确认完成的不同目标ID，不能把本就done的归并目标算作本次新向量。`SelectionSummary(eligible_total,selected,remaining)`。`RunRecord(schema_version=2,run_id,revision,rule_hash,sources,batches,completed_batches,group_ids,phase,counters,error,created_at,updated_at)`；`OperationRecord(operation_id,run_id,kind,request_hash,command,source_snapshot,selection,rule_hash,phase,revision,accepted_at,error)`，snapshot/selection对prepare冻结，rule_hash对恢复所需任务规则冻结（封存损坏任务允许为空）。
- `ConsoleCommand`为带kind判别的冻结联合：PrepareCommand(request_key,conversation_ids:tuple[int,...]|None)、ResumeCommand(request_key,run_id)、DecisionCommand(request_key,run_id,group_id,expected_revision,decision:Literal['approve','reject'],category?,reason?)、RetryVectorsCommand(request_key,run_id)、SealCommand(request_key,run_id,confirm_run_id,reason?)。kind分别为共享规则的五值。API的approve/reject在应用边界转成持久决定approved/rejected，不混用这两个枚举。source/message快照沿用既有frozen DTO，序列化后严格恢复。
- `ReviewStore.get_run(id)->RunRecord`、`put_run(record,*,expected_revision:int|None)->RunRecord`；组和操作提供同形的get_group/put_group、get_operation/put_operation；`list_runs()->tuple[RunRecord|AuditFault,...]`；`unfinished()->RunRecord|AuditFault|None`；`seal(run_id,operation_id,reason)->SealRecord`、`get_seal(run_id)->SealRecord|None`。put首次expected_revision=None且文件不能已存在；更新要求同ID当前revision匹配并加1。封存读取必须优先，损坏内容以AuditFault返回或显式报错，绝不跳过。
- `canonical_request_hash(command)->str`只覆盖客户端意图（包括默认选择模式；明确选择的ID先验重后排序规范化），服务器冻结来源另存，重试不能重新选当前来源。`normalize_category(value:str)->str`做NFC/strip、1–255字符并拒绝Unicode控制/格式字符；`parse_db_id(value:str)->int`只接受规范ASCII十进制1..2^63-1，不接受前导零、bool/数值或Unicode数字。旧原文不自动按新分类规则改写。`Page[T]`也定义于review_contracts，字段按共享合同。SealRecord同样由服务器记actor=本机操作，客户端不传可伪装的审核人。
- 有效queued操作若在run.json创建前崩溃，仍占未结束名额；查询用该冻结命令派生“interrupted/待继续”，只在明确继续时materialize运行记录，不能GET自动写或因run文件不存在创建第二个任务。真正损坏且无可用命令才是AuditFault。

- [ ] **RED：写真实文件/CAS行为测试。** 代表断言：

```python
def test_roundtrip_cas_and_seal_priority(tmp_path):
    store = make_store(tmp_path)
    first = store.put_run(make_run(), expected_revision=None)
    assert store.get_run(first.run_id).sources == first.sources
    with pytest.raises(ServiceError):
        store.put_run(first, expected_revision=None)
    store.seal(first.run_id, 'b'*32, '人工终止')
    assert store.unfinished() is None
```

另写`test_fsync_or_replace_failure_preserves_old_record`（故障后旧JSON逐字节不变）、`test_corrupt_record_blocks_new_run`、`test_idempotency_key_different_command_conflicts`、`test_nested_records_cannot_be_mutated`；参数表覆盖 ../、符号链接/junction逃逸、bool/浮点ID、2^63越界、分类空白/256字符/控制字符、tuple严格JSON往返。文件故障在OS边界注入，不mock掉ReviewStore本身。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_store.py -v`。Expected：缺新模块/行为的有效失败，记录日志；若只是fixture语法错，先修测试再记录有效RED。
- [ ] **GREEN：实现上述记录、CAS与原子存储。** 目录解析以resolve后的knowledge/reviews为根，输出字段全部校验；source/message ID不放入路径。封存文件即使run.json损坏也可确证，写失败仍阻塞名额。临时文件不当作有效任务扫描；未知合法记录损坏显式暴露。任务级汇总只派生，不代替组状态。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_store.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：相关测试及全套通过，无数据库/模型新写入演示库；保存实际日志与计数。
- [ ] **提交及留痕。** 限定上述4文件提交 `feat: persist strict knowledge review audit records`；当场记dev-notes，并在本计划账本记录BASE/命令/结果后标任务完成。

### Task 2: 审核专用MySQL读取、短事务应用和提交确证

**Files:** Create `retailq/knowledge/review_repository.py`、`tests/test_review_repository.py`；Modify `retailq/knowledge/review_contracts.py`（SourceSummary）、`tests/support/reviews.py`；只读复用原models/DDL，新增逻辑不放API。

**Interfaces:**
- Consumes任务1记录；`ReviewRepository(knowledge: KnowledgeRepository)`复用knowledge.database但每次自己创建短Session，不跨线程共享Session。
- `list_sources(*,query:str='',eligibility:str='all',offset:int=0,limit:int=20)->Page[SourceSummary]`、`source_detail(cid:int)->ConversationSnapshot`、`freeze_sources(ids:tuple[int,...])->tuple[ConversationSnapshot,...]`；SourceSummary含id/status/是否合格/原因、消息数与最新消息ID。freeze整次全有或全拒，空/重复/>100不接受。
- `categories(*,query:str='',offset:int=0,limit:int=20)->Page[str]`按Python精确值去重；`apply_group(group:ReviewGroup,decision:ReviewDecision,prepare:Callable[[PreparedApplication],None])->PreparedApplication`在SQL事务内flush目标ID后、commit前调用prepare；更新staging与pending知识同事务，不调用模型。
- `inspect_application(group:ReviewGroup)->Literal['committed','not_committed','conflict']`同时比对prepared目标指纹/全部成员预期状态；若无法确证按conflict。仅当extracted成员确证未应用才允许重新分配新ID，不能凭目标存在就成功。
- 测试支持新增`ReviewHarness(database,knowledge,repository,store,budget,embedding,vectors)`及`review_harness` pytest fixture：3307/retailq_test、真实Lite/tmp_path、FakeEmbedding/TestBudget，初始knowledge和集合为空（不套用会自动import_faq的旧service fixture）。`seed_closed_source(database,question,answer)->ConversationSnapshot`用真实SQL生成ID；`vector_count(h)->int`通过真实Lite SDK统计取得行数，具体API先核对当前PyMilvus官方定义；工厂不导入未来任务类。SourceSummary在review_contracts定义id/status/eligibility/reason/message_count/max_message_id字段。

- [ ] **RED：写事务/来源测试。**

```python
def test_prepared_failure_rolls_back_group_and_chunk(review_harness):
    h = review_harness
    group = seed_new_group(h)
    before = len(h.knowledge.all_chunks())
    with pytest.raises(OSError):
        h.repository.apply_group(group, approve('商品使用与保养'), fail_disk_write)
    assert len(h.knowledge.all_chunks()) == before
    assert all(row.status == 'extracted' for row in h.knowledge.staging(group_batch_ids(group)))
```

补`test_committed_application_is_confirmed_without_guessing_text`、`test_pending_member_and_occupied_prepared_id_not_treated_as_committed`、`test_member_drift_or_missing_target_conflicts`；来源覆盖进行中/已转人工、末tool/空assistant、不存在ID、多来源部分非法；SQL观察证明prepare前flush得到id，外部模型从不在本方法执行。分类覆盖大小写/重音不同不被ai_ci合并。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_repository.py -v`。Expected：缺新读取/事务方法失败，测试连接只到3307；3306连接配置即fixture拒绝。
- [ ] **GREEN：实现加锁核对与事务。** 通过新组只写人工最终category、完整questions/answer和pending；代表kept、其他同组成员discarded但全部有同一去向；旧目标归并只写staging，不修改目标文本/分类；拒绝只写discarded。prepare失败使事务回滚，提交前后故障分别保留可判断状态。数据库异常沿用脱敏ServiceError。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_repository.py tests/test_knowledge_repository.py -q`，再完整pytest。Expected：全部通过，DDL副本未变、原文/状态只在批准应用事务中发生变化。
- [ ] **提交及留痕。** 提交 `feat: apply reviewed knowledge groups in short transactions`，限定新仓储与测试/helper；写任务结果，不把本任务存储测试当语义评估。

### Task 3: 分类建议与只准备到待审核的挖掘引擎

**Files:** Create `retailq/knowledge/review_preparation.py`、`retailq/knowledge/review_classification.py`、`retailq/prompts/qa_category.txt`、`tests/test_review_preparation.py`、`tests/test_review_classification.py`；Modify `retailq/knowledge/qa_gateway.py`、`retailq/knowledge/review_contracts.py`（CategoryCandidate）、`tests/support/reviews.py`。暂不接入旧app/CLI，保留旧测试直到任务7迁移。

**Interfaces:**
- `ReviewPreparation(knowledge,repository:ReviewRepository,store:ReviewStore,gateway,embedding,*,input_budget:int,max_conversations:int=20,min_score:float=.85,candidate_k:int=8)`；`prepare(run_id:str,*,stop:Event)->RunRecord`消费已持久化queued命令中的快照，不重新读取当前会话。
- `rule_hash()->str`包括提取/去重/分类Prompt、schema版本、准备算法版本；按已有batch_conversations/extract_to_staging/global_dedup处理。本任务不调用apply_dedup、vectorize_pending或Milvus.upsert。
- `CategoryCandidate(group_id:str,questions:tuple[str,...],answer:str,categories:tuple[str,...],catalog_truncated:bool)`；`OpenAIQAGateway.classify(candidates:list[CategoryCandidate])->list[CategorySuggestion]`复用严格raw JSON/stop校验。旧extract/deduplicate接口不变。
- `classify_groups(groups:tuple[ReviewGroup,...],catalog:tuple[str,...],gateway,*,input_budget:int,stop:Event)->tuple[CategorySuggestion,...]`完整组/答案/Schema计预算，目录不足时记录truncated；不可分单组超预算则明确需要人工分类，留null并说明预算，不截答案；服务/格式失败仍是步骤失败。要求请求group_id集合恰好覆盖一次，拒绝伪造/重复/额外字段。
- staging batch号为sha256('review-v2:'+run_id+':'+batch_index)；组ID按共享规则。`processed_sources(store)->frozenset[str]`只排除确证已提取且其全部QA均已应用或人工拒绝的来源；零QA来源在提取完成后可排除。sealed中未处理完整的来源不能整体跳过。

- [ ] **RED：写准备与分类协议测试。**

```python
def test_preparation_stops_before_publishing(review_harness):
    h = review_harness
    run = queued_run_with_sources(h, two_equivalent_closed_sources(h))
    before = len(h.knowledge.all_chunks())
    result = make_preparer(h).prepare(run.run_id, stop=Event())
    assert result.phase == 'waiting_review'
    assert len(h.knowledge.all_chunks()) == before
    assert vector_count(h) == 0
    assert len(h.store.get_run(run.run_id).group_ids) == 1
```

helper的vector_count读取真实集合统计且h初始知识为空，不用假计数。另写`test_multi_batch_sources_keep_every_staging_member`、`test_prepare_resume_skips_persisted_batches`、`test_rule_change_refuses_resume`、`test_sealed_partial_source_can_be_selected_again`、`test_no_qa_completes_without_pending_review`；分类以真实LangChain+HTTPX MockTransport验证请求/返回字段，测重复/未知group ID、伪造decision/answer、截断JSON、null建议、超预算单组、目录截断标记。helper在本任务添加make_preparer/queued_run_with_sources等实际fixture，未来application/job fixture不得提前导入。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_preparation.py tests/test_review_classification.py -v`。Expected：新模块/方法未实现失败；Prompt语义只完成标注设计，协议RED不冒充真实模型评估。
- [ ] **GREEN：实现准备与分类步骤。** 逐批落暂存、持久化完成标记，全部批次处理后整体去重；去重候选临时三字段文本仍用category=对话知识，最终入库不沿用此占位值。分组建议只读既有目标；新组可建议已有/新业务类或留空，recommend_reject只显示建议。SourceQA.message_ids由服务器在已核验批次片段中计算：包含真实问法的user消息、完整答案/段落证据精确匹配的assistant消息；有多个匹配则都保留，不让LLM编消息ID。source_ref保留片段标记，能回到原run快照，保留每个成员来源。分类成功才标对应组步骤完成；stop在批次/比较/分类之间检查。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_preparation.py tests/test_review_classification.py tests/test_knowledge_extraction.py tests/test_knowledge_mining.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：新准备路径不发布；旧自动路径尚未改线，相关旧测试仍通过；本任务结果不能上线为完整功能。
- [ ] **提交及留痕。** 提交 `feat: prepare reviewable QA groups with category suggestions`；标注网络未跑、旧入口切换在任务6/7，避免中间提交被误认为已启用先审后入库。

### Task 4: 审批、确证恢复、指定chunk向量补齐与封存

**Files:** Create `retailq/knowledge/review_application.py`、`tests/test_review_application.py`；Modify `retailq/knowledge/review_contracts.py`（向量意图字段）、`retailq/knowledge/service.py`、`retailq/knowledge/repository.py`、`tests/support/reviews.py`（本任务加入application工厂）。

**Interfaces:**
- `ReviewApplication(knowledge,repository,store,service:KnowledgeService)`；`decide(operation_id:str,*,stop:Event)->RunRecord`、`resume_applications(run_id:str,*,stop:Event)->RunRecord`、`retry_vectors(run_id:str,*,stop:Event)->RunRecord`、`seal(operation_id:str)->SealRecord`。这些同步方法的调用者必须持维护租约；seal只在任务6确认工作线程离开安全边界后执行。
- `KnowledgeRepository.pending(limit:int,after_id:int=0,*,only_ids:tuple[int,...]|None=None)->list[ChunkRecord]`；`KnowledgeService.vectorize_pending(*,stop=None,fault=None,only_ids:tuple[int,...]|None=None)->VectorizeReport`，默认None保持旧行为，空tuple不处理任何块；仅对确认已批准的关联ID调用。
- `decide`从queued命令读取决定，在记录approved之前先用最终category构造完整ChunkDraft、校验原SQL长度和BGE完整三字段预算；校验失败只结束该操作，组仍待审核以便更正分类/拒绝，不能留下不可更正的批准。之后CAS写组decision，再repo.apply_group；prepare回调在同一组记录内写PreparedApplication。commit后确认applied，才离开SQL执行向量。错误持久化但不推断rollback已提交事实。向量失败仍保留approved/applied，重试不再次写知识。
- 本任务给ApplicationRecord增加vector_requested_ids/vector_confirmed_ids tuple，模型定义同步修改review_contracts.py：写向量前原子保存本次pending目标意图，回填done后确认；崩溃后可依据原意图与已确证状态恢复统计，已有done归并目标不冒充新写入。任务报告仍逐目标核对，不用计数替代内容指纹。
- `ReviewApplication.refresh_phase(run_id)->RunRecord`只派生汇总：全组应用完成且批准目标无pending/失败为completed；尚有未审为waiting_review；无法确证为conflict；sealed优先。新操作不得覆盖sealed。

- [ ] **RED：写用户可见一次应用/去向测试。**

```python
def test_approval_uses_human_category_and_same_id_on_retry(review_harness):
    h = review_harness
    run_id, group_id = prepared_new_group(h, suggested='商品使用与保养')
    command = queued_approval(h, run_id, group_id, category='售后手册')
    h.application.decide(command.operation_id, stop=Event())
    first = confirmed_target(h, run_id, group_id)
    h.application.decide(command.operation_id, stop=Event())
    assert confirmed_target(h, run_id, group_id) == first
    assert h.knowledge.get_chunks([first])[first].draft.category == '售后手册'
    assert h.knowledge.get_chunks([first])[first].vectorize_status == 'done'
```

另写`test_reject_has_no_target_and_preserves_source`、`test_merge_preserves_existing_text_category_and_vector_count`、`test_stale_or_opposite_decision_conflicts`、`test_invalid_final_budget_keeps_group_pending`、`test_sql_commit_before_audit_confirmation_recovers_same_id`、`test_prepared_only_and_occupied_id_is_not_committed`、`test_vector_failure_does_not_request_approval_again`、`test_seal_corrupt_run_keeps_raw_bytes_and_releases_slot`、`test_sealed_queued_decision_never_revives`、`test_partial_source_in_sealed_run_not_marked_fully_processed`。加only_ids=None/空/指定集合三组，确保任务补向量不碰无关pending。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_application.py -v`。Expected：缺新应用/窄范围向量接口失败，故障测试必须先观察到重复/错误确认等实际缺陷。
- [ ] **GREEN：按spec第8/9节实现恢复。** 检查member内容与状态、旧目标指纹，prepared落盘失败回滚SQL；提交确认失败后不能简单重试insert。空/不存在目标、混杂状态/人工漂移为conflict。封存独立标记优先，拒绝后续操作，保留所有已有字节和MySQL/向量；只停止本任务，既有全局vectorize对已批准pending仍按旧合同工作。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_application.py tests/test_review_repository.py tests/test_knowledge_service.py -q`，再完整pytest。Expected：真实MySQL/Lite下ID、状态和确证映射一致；同进程故障注入先通过，真实进程kill在任务10独立取证。
- [ ] **提交及留痕。** 提交 `feat: publish approved knowledge with recoverable provenance`；记录已验证的是应用/存储语义，不是云端正确分类。

### Task 5: 只读工作台投影及旧版去向边界

**Files:** Create `retailq/knowledge/review_queries.py`、`tests/test_review_queries.py`；Modify `retailq/knowledge/review_contracts.py`（安全查询DTO）、`tests/support/reviews.py`（加入queries工厂）。

**Interfaces:**
- `ReviewQueries(repository,knowledge,store,legacy_root:Path)`；`summary()->ConsoleSummary`、`sources(...)->Page[SourceSummary]`、`source(cid:int)->ConversationSnapshot`、`categories(...)->Page[str]`、`runs(...)->Page[RunSummary]`、`run(run_id:str)->RunDetail`、`groups(run_id,*,query='',disposition='all',category='',offset=0,limit=20)->Page[GroupSummary]`、`group(run_id,group_id)->GroupDetail`、`chunk(cid:int)->ChunkDetail`、`operation(operation_id)->OperationView`、`legacy_run(run_id)->LegacyRunDetail`。
- 这些为无写入、无模型/向量查询的投影：安全错误、所有来源/消息、原文/最终category、group revision、pending/approved/rejected、application phase、确认target/每成员去向、当前MySQL vectorize_status。prepared ID标为planned_target_id且target_confirmed=false；仅applied确证为target_chunk_id。summary含counts、current_run_id/phase及vector_verification=not_checked，不宣称即时核验了Milvus。
- API安全DTO只列这些可见字段，不透传整个OperationRecord/本地路径/配置。LegacyRunDetail只读，旧去重组显式target_chunk_id可核对则标legacy_explicit，其余target未知；不能用文本相同自动补链接。AuditFault单独展示，故障不能被空态掩盖。
- 在review_contracts定义查询DTO：ConsoleSummary(chunks,counters:RunCounters|None,current_run_id,phase,vector_verification,error)、RunSummary(run_id,kind,phase,counters:RunCounters|None,created_at,error)、RunDetail(summary,sources,groups_count)、GroupSummary(group_id,revision,question,disposition,decision,category,suggestion)、GroupDetail(summary,members,questions,answer,target_confirmed,planned_target_id,target_chunk_id,application_phase,vectorize_status,error)、ChunkDetail(record:ChunkRecord)、OperationView(operation_id,run_id,kind,phase,error)、LegacyRunDetail(run_id,phase,sources,explicit_targets,missing_provenance)。损坏/封存但无法核对的来源和计数允许null，页面显示未知/故障，不能填0伪装空任务；完整组无法读取则返回安全错误。DTO构造时决定可见字段，不以asdict整份磁盘记录代替投影；API主键序列化由任务8统一。

- [ ] **RED：写真实记录投影测试。**

```python
def test_prepared_is_not_a_confirmed_destination(review_harness):
    h = review_harness
    rid, gid = seed_prepared_without_commit(h)
    view = h.queries.group(rid, gid)
    assert view.target_confirmed is False
    assert view.target_chunk_id is None
    assert view.application_phase == 'prepared'
```

另测新组多个成员都指同chunk、人工拒绝与discarded同组归并的原因不同、旧新同run ID命名空间隔离、legacy明确目标变化变unknown、损坏/缺审计不返回空成功、GET全程SQL只读且不触发embedding、分页/filter/canonical ID规则。用SQL事件和组件实际数据观测，不mock掉queries。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_queries.py -v`。Expected：缺新投影或错误target语义失败。
- [ ] **GREEN：实现投影。** 每次短SQL回读权威原文，安全映射，不缓存将prepared“升级”为applied；遇到跨文件revision变化可重读一次，否则显示processing，不编造一致快照。类别在Python精确匹配，过滤只属管理页，不成为生产检索召回通道。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_queries.py tests/test_review_store.py tests/test_review_repository.py tests/test_review_application.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：无写入/模型调用，分页数量可复验，旧版缺关联明确可见。
- [ ] **提交及留痕。** 提交 `feat: expose auditable knowledge review read models`。

### Task 6: 唯一维护准入、后台作业与关闭资源顺序

**Files:** Create `retailq/knowledge/review_jobs.py`、`tests/test_review_jobs.py`；Modify `retailq/knowledge/runtime.py`、`retailq/app.py`、`tests/test_knowledge_runtime.py`、`tests/test_knowledge_chat.py`、`tests/support/reviews.py`（本任务加入job_env/受控线程fixture）。

**Interfaces:**
- `MaintenanceLease.close()->None`幂等释放维护锁和runtime活动计数。`Admission(operation:OperationRecord,lease:MaintenanceLease|None)`：同操作已有工作线程或处于终态时返回lease=None，不创建第二线程；若相同请求是用户明确重试的queued/interrupted命令且无人持有，可重新取得原操作租约，不生成第二份决定/快照。
- `KnowledgeRuntime.admit_review(command:ConsoleCommand)->Admission`：同进程一个准入锁，先查request key精确复用，再检查sealed/逻辑未结束run/组版本与维护占用；冻结来源并持久化queued操作后才受理。所有外部模型等待都在execute阶段。新prepare已知pending run时返回继续信息或409，不创建新run。
- `KnowledgeRuntime.execute_review(operation_id:str,lease:MaintenanceLease,stop:Event)->OperationRecord`：使用已取得租约，不二次抢维护锁；路由prepare/resume/decide/retry_vectors/seal，finally释放。`reserve_maintenance()->MaintenanceLease`供原import/vectorize/reconcile共用。属性`review_store`、`review_application`、`review_queries`在已启用runtime中可用，`review_preparation:ReviewPreparation|None`及`can_prepare_review:bool`表示抽取/分类模型能力。只在需要模型的prepare/对应resume阶段缺网关时报model_not_configured，不能因此禁用已经待审内容的拒绝/通过、封存、只读查询或独立向量补齐。
- `KnowledgeRuntime.queue_review_seal(command:SealCommand)->OperationRecord`在准入控制锁内持久化封存申请并标记该run待封存，不抢正被使用的维护锁；它不写SQL/组终态。此后普通新维护操作409，已在运行worker仅完成当前安全边界。`ReviewJobs(runtime)`提供`async submit(command)->OperationView`、`async resume_queued(operation_id)->OperationView`、`async seal(command)->OperationView`、`async aclose()->None`。拥有Task/stop Event，后台to_thread运行execute；屏蔽请求取消对已受理Task的取消，但关闭时确实等线程。seal先queue并设stop，再等待活跃lease释放，随后对同一queued seal命令admit并execute写终态；多个相同申请共用原命令，不能忙碌即丢终止请求。工作线程/运行租约登记在runtime，不仅登记在某个HTTP请求中，暂经scheduler调用的worker也能被安全等候。
- 启动发现遗留操作时，只标interrupted。用户明确resume任务时，若run.json尚不存在，则从原prepare命令的冻结快照/规则哈希materialize；已有未应用decide命令按受理时间在同一维护租约内确证/补完，sealed先过滤，规则哈希变化禁止重跑模型，不重新询问或覆盖旧批准。受理时完成同请求/同组既有决定复用检查，不能把明确重试当作新决定。
- lifespan构造并放app.state.review_jobs，KnowledgeRuntime构造新增显式review组件，删除对原pipeline自动发布的调用；app原`knowledge.pipeline`可用性判断改为can_prepare_review，测试替身同步按真实能力合同调整。原app scheduler可暂沿runtime.maintain，但任务7统一到jobs；两者已共用admit门禁。

- [ ] **RED：用Event控制真实线程测试。**

```python
@pytest.mark.anyio
async def test_shutdown_waits_for_admitted_worker_before_closing_resources(job_env):
    env = job_env
    await env.jobs.submit(env.prepare_command)
    assert await asyncio.to_thread(env.entered.wait, 2)
    closing = asyncio.create_task(env.jobs.aclose())
    assert not env.resources_closed.is_set()
    env.release.set()
    await closing
    assert env.worker_exited.is_set()
```

另测同key重复没有第二worker、异key繁忙/旧revision409、202对应命令已可从磁盘读、worker开始前kill的queued可识别、请求取消后仍执行、sealed申请等线程后才落终态、关闭时新admission拒绝、等待审核不占维护锁/SQL、只有一个Lite.open、FAQ可在管理线程模型等待时运行。测试不用sleep碰时序。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_jobs.py -v`。Expected：缺准入/作业组件或资源提前关闭的有效失败。
- [ ] **GREEN：接线运行组件。** 读lease与维护lease分别计数，租约所有权/operation必须匹配，释放仅一次；持久记录后线程创建失败标interrupted且释放，不能返回假成功。启动只把遗留queued/running标interrupted，不自动恢复写操作。app关闭顺序为chat清理→jobs drain→scheduler stop→runtime→DB→model，jobs开始关闭后scheduler提交也拒绝。保留禁用知识时不import PyMilvus的既有修复。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_jobs.py tests/test_knowledge_runtime.py tests/test_knowledge_chat.py tests/test_tool_chat.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：原工具/生命周期合同仍通过；所有实际对话挖掘入口已经不走旧自动应用类，旧文件清理和旧测试迁移在任务7。
- [ ] **提交及留痕。** 提交 `feat: own review jobs in the shared knowledge runtime`。

### Task 7: CLI与scheduler统一进入人工审核，退休旧自动发布流程

**Files:** Modify `retailq/knowledge/mining.py`（保留atomic_json等被scheduler调用的工具，移除自动MiningPipeline发布实现）、`scripts/knowledge.py`、`retailq/knowledge/scheduler.py`、`retailq/app.py`、`tests/test_knowledge_cli.py`、`tests/test_knowledge_mining.py`、`tests/test_knowledge_runtime.py`；Create `tests/test_review_entrypoints.py`。

**Interfaces:**
- `runtime.maintain('mine',options,stop)`用admit/execute同步准备或恢复；CLI `--resume`只认版本2，旧ID明确knowledge_legacy_readonly。未指定来源的CLI/scheduler只按ID升序选择最早最多100个尚未完整处理的合格来源，报告eligible_total/selected/remaining，避免把上限截取伪称已处理所有来源。
- CLI mine输出保留run_id/extracted/kept/discarded/vectorized/complete，并增加phase、review_pending、selected/remaining。waiting_review时complete=false但退出0；真正执行失败1、配置/不兼容2；空来源completed/complete=true。status仍明确vector_verification=not_checked。
- `DailyKnowledgeScheduler(runtime,jobs:ReviewJobs|None=None,*,clock=None)`；应用运行使用jobs.submit，CLI无jobs仅同步维护。等待审核是正常准备完成，schedule记录phase=waiting_review，complete=false和run_id；当天不重复触发；重启已有pending审核不会抽第二次。用户当前关闭配置不改变。

- [ ] **RED：入口越权回归。**

```python
def test_cli_mine_waits_for_review_with_zero_exit(cli_review_env):
    env = cli_review_env
    before = env.chunk_ids()
    assert env.invoke(['mine']) == 0
    report = env.last_report()
    assert report['phase'] == 'waiting_review' and report['complete'] is False
    assert env.chunk_ids() == before
```

补scheduler两次/重启一次不发布、pending run不重复选来源、CLI/scheduler/page同时触发同门禁、>100明确remaining、旧run拒绝resume、仅部分应用sealed来源可重选。原test_knowledge_mining中的批次恢复/去重算法测试继续保留；原自动发布恢复测试迁至review_application/任务10语义，不直接删掉覆盖。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_entrypoints.py tests/test_knowledge_cli.py -v`。Expected：旧CLI把waiting_review当失败或旧自动发布行为暴露；记录实际失败。
- [ ] **GREEN：移除所有旧自动发布调用。** import/vectorize/reconcile保持原合同；scheduler通过同一jobs受理，stop等真实worker。测试搜索并审阅所有MiningPipeline.run/apply_dedup引用，只保留明确经审核的底层仓储测试或新应用代码，不留UI外自动发布开关。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_entrypoints.py tests/test_knowledge_cli.py tests/test_knowledge_runtime.py tests/test_knowledge_extraction.py tests/test_knowledge_mining.py tests/test_review_application.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：无对话挖掘可越过批准；原纯Markdown/FAQ维护与查询仍工作。
- [ ] **提交及留痕。** 提交 `feat: require review for every conversation mining entrypoint`；当场记CLI等待状态/退出码有意变化，更新开发说明中的旧自动流程描述。

### Task 8: 本机管理API、严格DTO与同源写入边界

**Files:** Create `retailq/api/knowledge.py`、`retailq/api/knowledge_schemas.py`、`retailq/api/knowledge_access.py`、`tests/test_review_api.py`；Modify `retailq/app.py`。

**Interfaces:**
- APIRouter前缀/api/knowledge。安全response模型只投影任务5字段，所有DB ID序列化为十进制字符串；body/参数用严格模型，额外字段422，不把客户端字段直接传仓储。
- `require_console_local(request)->None`检查实际client.host为loopback、URL主机只为localhost/127.0.0.1/::1；不信任Forwarded/X-Forwarded-*。`require_console_write(request)->None`再要求Content-Type application/json、`X-RetailQ-Console: 1`、Origin与当前scheme/host/有效port相同，缺失/空/null/不同端口403；不改旧聊天路由限制。runtime未ready为503，静态页仍可读。
- GET：/summary、/sources、/sources/{conversation_id}、/categories、/runs、/runs/{run_id}、/runs/{run_id}/groups、/runs/{run_id}/groups/{group_id}、/chunks/{chunk_id}、/operations/{operation_id}、/legacy-runs/{run_id}；列表offset默认0/limit20上限100，参数query≤200、eligibility=all|eligible|ineligible、disposition=all|new|merge|rejected|pending、category≤255。
- POST /runs：`request_key,conversation_ids`；/runs/{id}/resume及/retry-vectors：request_key；/runs/{id}/seal：request_key,confirm_run_id（必须同path）；/runs/{rid}/groups/{gid}/decision：request_key,expected_revision,decision=approve|reject,category?,reason?≤512。approve新组必须category，merge不得传category，reject不得带category。首次受理202；相同已完成命令200复用，已在执行的相同命令202；同组相同最终决定可复用既有结果，矛盾或变更理由/类别409。所有返回带operation_id/run_id供轮询。

- [ ] **RED：接口边界与数据流测试。**

```python
def test_decision_api_persists_command_before_202(api_env):
    env = api_env
    response = env.client.post(env.decision_url, json=env.approval, headers=env.same_origin_headers)
    assert response.status_code == 202
    operation_id = response.json()['operation_id']
    assert env.store.get_operation(operation_id).kind == 'decide'
```

使用真实TestClient(app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',50000))启动lifespan，替模型不替路由/协调器。另测远端client/HostDNS重绑定/伪XFF、跨Origin/无Origin/无管理头、重复/非法/布尔ID、超过100、9007199254740993不舍入、损坏记录与旧ID不写、未ready503但/health原合同、202后断连仍运行、相反决定409。403请求应没有新审计命令或MySQL变化。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_api.py -v`。Expected：新路由404或边界行为失败，不改测试Host来放宽生产限制。
- [ ] **GREEN：实现薄API。** 读取转queries，写入转jobs，转换ID与校验在DTO边界完成；不打开第二runtime、不在GET中跑恢复。loopback检查同时用于/knowledge静态入口，错误按既有安全envelope；不加全局TrustedHost改变旧API合同。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_api.py tests/test_contracts.py tests/test_chat_api.py tests/test_knowledge_chat.py tests/test_review_jobs.py -q`，再 `& $ProjectPython -m pytest -q`。Expected：标准管理请求能完成实际后台状态变更；非授权来源完全无写入；旧接口合同不变。
- [ ] **提交及留痕。** 提交 `feat: add local knowledge review management API`。

### Task 9: 将确认过的草图实现为真实管理页面

**Files:** Create `retailq/web/knowledge.html`、`retailq/web/knowledge.css`、`retailq/web/knowledge-api.js`、`retailq/web/knowledge.js`、`retailq/web/knowledge-views.js`、`tests/test_review_web.py`、`tests/support/review_console_server.py`；Modify `retailq/web/index.html`、`retailq/app.py`、`tests/support/database.py`。

**Interfaces:**
- GET /knowledge返回独立HTML（Cache-Control:no-cache），聊天页“知识工作台”链接及管理页“返回客服”链接；样式独立作用域，不重构聊天app.js。字体/图标用现有系统资源，无外部资源/CSP绕行。
- knowledge-api.js导出`consoleRequest(path,{method='GET',body,signal}={})`，相对/api/knowledge，写请求统一管理头和JSON，错误保留安全code/message；`newRequestKey()`用crypto.randomUUID移除连字符。DB ID始终字符串，不能Number/parseInt。
- knowledge.js管理sources选择、待审组、category、新类、decision请求键、operation轮询和恢复/封存状态。活跃约1000ms、待审/空闲5000ms，失败指数退避上限30000ms；切页清理计时器，document.hidden暂停轮询但不取消任务，重新可见刷新真实状态。重复提交禁用按钮并复用同key；超时不声称批准失败或自动发反向决定。
- knowledge-views.js只通过DOM节点/textContent渲染；分组多来源可展开，每个来源可查看冻结消息；新增与归并分别呈现最终category/确认target，prepared/旧版unknown/拒绝不能假链到chunk。approve新组和merge的输入规则严格匹配任务8。
- 本任务同时建立可被CUA访问的测试服务：`python -m tests.support.review_console_server --scope test --output DIR --port 8765`，强制3307/retailq_test与独立artifacts/verification/ch03-review/browser-*目录。抽出tests/support/database.py中已有隔离bootstrap为`reset_isolated_database(settings)->Database`供fixture与该服务共用，任何非loopback/3307/retailq_test配置在写入前拒绝；它会清理可丢弃的隔离库，绝不碰3306。脚本只替换模型/嵌入边界，使用真实KnowledgeRuntime.open/app/API/MySQL/Lite/审计组件；不patch协调器或路由、不加生产测试分支。每次浏览器验证结束先停止服务、等资源释放再运行pytest/恢复脚本，禁止同一测试库并行重置。

- [ ] **RED/浏览器失败基线：真实页面路由与行为。** 写`test_console_page_and_navigation_exist`，assert /knowledge=200且实际导航可达；先用用户确认草图作布局参考，不以源码常量测试按钮逻辑。缺页面的404作为自动RED；本任务GREEN中的测试服务可访问后，再用真实浏览器逐项检查交互，有缺口先记录实际失败再修，不借用尚未编写的任务12服务。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_web.py -v`。Expected：页面404/入口不可达；同时记录浏览器初始失败，不能用纯HTML存在替代交互验证。
- [ ] **GREEN：实现已确认布局。** 顶部统计/来源弹窗、待审/去向/任务页签、人工分类覆盖、逐组通过/拒绝、实际进度/错误/重试、明确封存确认。删掉草图全部固定业务统计/示例ID/假成功toast；空数据真实空态。未配置仍显示可理解的原因；当前待审任务优先“继续审核”，不能假开始另一个。拒绝/归并原因不同，已有目标继续可检索。新类在人工提交前只存在本地输入，不改变分类目录。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_web.py -q`及 `& $ProjectPython -m pytest -q`；之后启动本任务测试服务，真实浏览器点一次来源选择/分类覆盖/新类/拒绝/归并/查看目标/重复点击/封存。输入`<img src=x onerror=...>`及DSML来源按纯文本显示、无脚本执行；9007199254740993目标链接和request body逐字一致。宽屏并排/窄屏纵排且按钮可达，console无新增error。截图与实际API反馈存artifacts/，不把草图验证当此任务证据；本步结束停止测试服务再完成任务。
- [ ] **提交及留痕。** 提交 `feat: build the knowledge review workbench UI`；记录每个已实测操作及仍未跑的云端场景。

### Task 10: 真正跨进程中断与审核审计恢复验收工具

**Files:** Create `tests/support/review_worker.py`、`scripts/verify_knowledge_review_recovery.py`、`tests/test_review_recovery.py`。

**Interfaces:**
- `python -m scripts.verify_knowledge_review_recovery --scope test --output DIR --embedding-mode deterministic|live`。只允许loopback:3307/retailq_test，DIR resolve后必须位于项目artifacts/verification/ch03-review/下的独立新run目录；根目录、演示knowledge目录、路径逃逸或已有不同run目录全部拒绝。live不由开发侧擅自执行。
- worker继承上述硬门禁，打开真实DirectoryOwner/MySQL/Lite，准备专用来源/任务。文件IPC ready包含stage/worker PID/operation ID；父进程确认边界后终止该PID、wait确认退出，再新进程恢复同目录/同命令。不得仅raise异常冒充kill。只观测/操作本次标记的来源和ID，记录已有行/ID基线，不清演示库或删卷。
- 故障点：after_decision_persisted、after_prepared_before_sql_commit、after_sql_commit_before_applied、after_milvus_upsert_before_mysql_done；前三点批准/拒绝均覆盖，向量点仅批准。另测queued受理后worker启动前kill、seal在损坏run上重启优先、申请封存遇活跃应用线程先等安全边界。
- 报告含embedding_mode、实际嵌入配置（deterministic必须标deterministic-test）、chat_mode=fixture（恢复场景固定样例问答，不冒充真实分类Prompt）、所有命令/阶段、前后chunk/staging ID/状态/确证去向、向量数及pending数、run/操作完整性；失败保留。live仅指真实BGE嵌入，LLM语义另由任务11评估。退出0仅全部指定场景通过，1失败，2缺配置/范围不合法。

- [ ] **RED：隔离与断点门禁。**

```python
def test_recovery_refuses_demo_database_and_knowledge_directory():
    with pytest.raises(ServiceError):
        validate_recovery_scope(demo_settings(), Path('artifacts/knowledge'))
```

另写`test_parent_waits_for_worker_exit_before_reopen`、`test_killed_committed_group_has_same_confirmed_target_after_restart`、`test_killed_prepared_transaction_not_misread_as_applied`、`test_sealed_queued_command_stays_sealed_after_restart`；实际subprocess/IPC，不mockprocess终止结果。限制单用例超时与统一清理子进程，输出不得打印.env/密钥。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_recovery.py -v`。Expected：缺脚本/边界失败；真实DB不可用明确失败，不skip伪通过。
- [ ] **GREEN：实现工具与测试worker。** 注入故障只在tests/support worker侧包裹公开边界，不给生产HTTP增加kill/任意fault参数。确定性模式用明确不同的模型身份与证据目录；live使用原BGE/tokenizer和用户设置，任何失败不换模型。对每个审计结果检查所有成员和指纹，不只计数。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_recovery.py -q`；再 `& $ProjectPython -m scripts.verify_knowledge_review_recovery --scope test --embedding-mode deterministic --output artifacts/verification/ch03-review/recovery-local-01`（目录已存在则新run名），最后完整pytest。Expected：所有真实kill边界恢复、ID不重复、fake报告明确；live部分生成用户命令但不运行，保持未验证。
- [ ] **提交及留痕。** 提交 `test: verify review recovery across process termination`；分别记录真实进程/存储证据与尚无真实BGE网络证据，不混为一个通过结论。

### Task 11: 分类Prompt标注评估与用户反馈采集工具

**Files:** Create `scripts/evaluate_knowledge_categories.py`、`scripts/smoke_knowledge_console.py`、`evals/ch03_console_categories.jsonl`、`tests/test_review_evaluation.py`。

**Interfaces:**
- `python -m scripts.evaluate_knowledge_categories --cases PATH --output DIR`调用已配置聊天网关分类、落完整实际输出；`--review REPORT`只读汇总已人工标注结果。报告固定case ID全集、run_completed、Prompt/hash/配置模型名、目录是否截断、原问答、实际建议/理由/recommend_reject、安全错误、review.status=pass|fail|pending及具体reason。声明模型名是配置值，不独立证明远端部署身份。
- 标注集至少12例：已有category优先、新业务类别、无明确类别null、单次订单建议拒绝、上下文注入、不同适用条件、同义多问、超长目录、预算边界与新分类经人改为已有类等。参考标签可含合理候选集合，最终由逐例理由审核，不让脚本因标签字符串相同自动判全通过。
- `python -m scripts.smoke_knowledge_console --base-url URL --run-id ID --output FILE`只允许本机URL，只GET已存在真实任务/组/目标/操作投影，保存来源/决定/去向及当前状态，绝不代用户点击通过、触发模型或修改审计。页面尚无对应任务则明确无证据。两脚本输出原始失败报告不覆盖；0全部通过、1失败、2环境缺失/待人工评。

- [ ] **RED：评估门禁与只读采集。**

```python
def test_unreviewed_or_incomplete_category_report_cannot_pass():
    report = category_report(review_status='pending')
    assert summarize_category_report(report)['status'] == 'pending'
    with pytest.raises(ValueError):
        summarize_category_report(report_with_duplicate_case_ids())
```

另测缺例/额外例、无理由、未完成、规则哈希不匹配、remote base-url拒绝、collector只发GET且不改记录。对12例标注逐项核对完整来源/预期，不把数据文件行数当语义评估。
- [ ] **运行RED。** Run `& $ProjectPython -m pytest tests/test_review_evaluation.py -v`。Expected：缺工具/错误门禁失败。
- [ ] **GREEN：实现报告和样例。** 复用已有严格schema/CLI安全输出风格；不要复制网络重试逻辑或内嵌密钥。原例未通过修Prompt时新输出run重测，保留旧失败；没有真实模型结果就保持pending，不用MockTransport补上实际语义分数。
- [ ] **验证。** Run `& $ProjectPython -m pytest tests/test_review_evaluation.py -q`及 `& $ProjectPython -m pytest -q`。Expected：门禁和只读采集通过；脚本实际云端分类由用户运行，提供准确命令，不在开发侧试代理。已有“电子说明书”单例只作为先前版本证据，不能算新版人工审核/分类Prompt已通过。
- [ ] **提交及留痕。** 提交 `test: add category review evaluations and console evidence capture`。

### Task 12: 真实浏览器整链路、操作文档与整体回归

**Files:** Create `docs/ch03-knowledge-console.md`；Modify `tests/support/review_console_server.py`（补完整场景）、`README.md`、`docs/ch03-manual-acceptance.md`、`dev-notes/ch03.md`；必要的集成断言放`tests/test_review_api.py`/`tests/test_review_web.py`。

**Interfaces:**
- 复用任务9已建立的开发浏览器服务 `python -m tests.support.review_console_server --scope test --output DIR --port 8765`，补齐多批/拒绝/归并/损坏与封存场景；同一时刻不与pytest/恢复脚本共用隔离数据库。凭据不输出，终端日志给本机URL，进程持有资源并可正常关停，不在生产app加测试后门。
- 用户正式入口仍为单worker `uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 1` + /knowledge。文档说明先审后入库、默认关闭scheduler、浏览器无需停机但外部CLI仍停机维护、封存非回滚、旧版去向缺失、MySQL与整个关闭后knowledge目录成套备份。
- README改为当前功能说明，不让旧章“query_faq为LIKE/不存在RAG”等历史段落继续作为现状；保留原章历史评估链接并标时期，不改写旧报告。演示规则始终注明演示。

- [ ] **RED/浏览器失败门禁。** 对完整API应用写`test_unreviewed_qa_is_not_retrievable_then_reviewed_category_and_source_survive_reopen`：未审新知识无原文/向量，通过后真实ID/分类可回取，关闭重开仍有审计链接；`test_seal_and_restart_release_slot_without_reviving_approval`。若现有实现已满足，不捏造RED，记录该集成行为由先前已红绿测试覆盖；发现任何缺口先新增有效失败再修。
- [ ] **运行集成测试。** Run `& $ProjectPython -m pytest tests/test_review_api.py tests/test_review_web.py tests/test_review_jobs.py tests/test_review_recovery.py -q`。Expected：全部通过；否则按systematic-debugging定位，不能靠扩大允许Host/忽略API错误放行。
- [ ] **运行真实浏览器验收。** 使用现有CUA浏览器工具打开受控本机app，实点来源选择→抽取至waiting_review→对照多来源→覆盖建议类别→批准/拒绝/归并→查看真实chunk/向量状态→刷新恢复→封存确认→新建任务；并回客服页验证FAQ仍工作。当前无来源空态、非合格禁选、旧版unknown、XSS纯文本、BIGINT、双击和关页不中断已受理命令均留实际页面/响应证据；临时viewport测试后恢复，不用草图状态当结果。云端真实链路留给用户按新文档执行并反馈。
- [ ] **最终本地验证与说明。** Run完整pytest、pip check、原DDL源/副本SHA256、git diff --check；复用前序干净runtime/dev锁环境，不重建或安装网络依赖：runtime锁环境做pip check/必要导入，dev锁环境做pip check/完整pytest，不能要求不含pytest的runtime环境跑测试。Expected：所有本地命令成功，实际计数/差异记录；若现有干净环境已不存在或有漂移，明确报告并遵守先解释环境策略的要求，不能伪称干净锁已验证。记录用户尚未反馈的真实分类/网页/恢复场景，不将本地替身说成已通过真实验收。
- [ ] **提交及留痕。** 提交 `docs: document the knowledge review console and verified local flows`；完成本计划全部任务的代码/本地验证账本。下一节整体审查/finish仍是必要门禁，不能仅因12任务提交就声称全功能finish。

## 全部任务之后：一次整体代码审查与finish

按保留的Native执行方式，任务12完成后，生成本计划完整base/head审查包；从产品基点87a3660覆盖本次功能及已有DSML防护的兼容边界，明确不把前序尚未做的完整章节审查伪称已完成。采用requesting-code-review的code-reviewer.md，在最有判断力的可用模型上派一个新上下文整体审查者，携带批准spec、此计划、实际验证与账本所有Ruling、上面五项Review Focus原文。不要派每任务审查者或另派计划审查代理。

收到结论先按用户影响重新分级。Critical/Important由作者一次红→绿修复并跑完整回归，已解决用测试证明，不派第二次独立复审；Minor记deferred，不混入必需修复。对“Declined to judge”与未修复项做明确Ruling，记录影响和代价，不把无结论项当通过。

当场追记dev-notes code review结论、修复与未验证边界；使用verification-before-completion/finishing-a-development-branch，按既定main约定限定文件提交、不自动推送。只有实际门禁都达到才记finish；如果真实分类/业务链路还待用户执行，则交付当前已实现/本地验证和待验收项，不把它写成全量真实验收通过。

归档必要的brief/红绿/审查证据到适当文档或artifacts之后，按执行技能只清理本计划的专有scratch，先核对绝对路径，不动旧计划/用户运行数据。最终交付页面入口、完整启动/演示/反馈命令、测试结果、spec/plan/review/dev-notes路径，以及所有执行Ruling和Deferred minors。

## 作者自检结论（2026-10-27）

- Spec覆盖：1/2/3→header与全局；4→8/9/12；5/6→2/3/11；7/8→1/2/4/5/10；9→4/6；10→7；11→5/8/9；12→文件职责/文档依据；13→所有验证及10/11/12；14→共享规则与执行Ruling。
- 跨任务类型：以任务1领域记录、任务2仓储、任务3准备、任务4应用、任务5查询、任务6准入/作业为唯一来源；API/UI不重命名阶段，不自行推断目标。prepare执行不发布；query不恢复写入；decision不改问答；seal优先于queued。
- 文档依据：Context7核对FastAPI Request/TestClient/路由、Pydantic精确2.14.1 strict/frozen/JSON；SQLAlchemy的Context7返回2.0，沿批准spec列出的2.1官方Session核对。具体方法实现前继续按安装版本查官方接口，不凭本文替代文档。
- 已由作者执行writing-plans五项检查：14节spec均有任务归属；12任务各5个步骤、每个接口有生产/消费任务；五项Review Focus均有命名测试；篇幅约为spec的2.3倍，主要为接口、边界和验证，未预写完整实现。代码示例只作语法检查，不冒充执行过的测试。
- 自检修正：把浏览器受控服务器前移到首次需要它的任务9；澄清operation_id字段、查询DTO、审批approve/approved枚举、临时向量/本次写入统计；queued无run文件仍占名额；终止请求独立排队且等安全边界；审核/封存不因缺聊天模型被无谓禁用；普通维护与所有挖掘入口同一准入；分类/源消息ID不由模型编造；超预算最终草稿在批准前拒绝，保留可更正状态。
- 本次只由作者自检，未派计划审阅子代理；前一阶段builder check是设计检查，不改称计划审查。实际结构计数、示例AST结果和提交记入dev-notes；等待用户审阅计划后按已选Native执行。
