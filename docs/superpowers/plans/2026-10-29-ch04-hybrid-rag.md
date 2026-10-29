# ch04 Hybrid RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有客服中交付Milvus原生混合检索、指定模型重排、可追溯引用与拒答入池，并以真实四策略评估和聊天页交互证明功能。

**Architecture:** 升级既有query_faq，线上、CLI和评估共用检索与证据模块。MySQL保留原文及新问题池/台账，活动语料清单和版本化Standalone集合共同控制检索生效；共享本机目录中的分作用域OS租约协调多worker、审核与建库。RAG答案缓冲自评后发送，聊天页保留原生HTML/CSS/JS。

**Tech Stack:** Python 3.12.13既有`.venv`、FastAPI 0.159.1、SQLAlchemy 2.1.6/MySQL 8.4、langchain-core 1.6.13/langchain-openai 1.7.2、PyMilvus 3.0.2 + Milvus Standalone 3.0.2、BAAI/bge-m3（1024/COSINE）、BAAI/bge-reranker-v2-m3、httpx 0.28.1、原生浏览器。

**Spec:** `docs/superpowers/specs/2026-10-29-ch04-hybrid-rag-design.md`，用户2026-10-29批准。计划基点main/7dfde94；当前仅编写计划，以下checkbox均未执行。本文等待用户审阅与执行方式确认，不能把设计批准当成计划批准。

## Global Constraints

- Milvus Standalone Docker；原生BM25、text字段BM25 Function、内置 `chinese` analyzer。版本至少2.5，本计划冻结服务端/SDK为3.0.2/3.0.2，不改模型、服务商或检索路线。
- dense与BM25各召回Top-50，`hybrid_search` + `RRFRanker(k=60)`融合候选Top-50；`BAAI/bge-reranker-v2-m3`精排Top-10。布局 `[1,3,5,7,9,10,8,6,4,2]`，不足10时对相关性排名按同样奇数升序/偶数降序规则，编号按实际prompt顺序重建。
- 原话和型号/数字/否定/条件保真；只做单轮归一，不做指代消解、多轮改写、二次检索或Agent Loop；同义词只在检索侧扩展，不拆存多份。
- 分类/类型白名单过滤同时下推dense/BM25；未知分类空结果，不放宽；MySQL原文、活动版本、done和指纹一致才可作为证据。
- 运费统一满99元免基础运费、未满10元，保留偏远例外。参考人设/品牌“小喵客服、喵喵购物、喵喵优选”不进入新知识；资料不当成真实企业政策。
- 用户DDL原样复制，SHA-256=`8840159feb482e325db6b052e99eb646e1758d7bfe3489d7ab184e535b0df118`；只新增low_confidence_questions、faith_cases，不擅改既有表或增加协调表/列。
- useful=false/无证据/低相关性拒答，分别以self_check/retrieval_low_conf入池；基础设施/格式错误走error，不能当知识缺口。拒答和入池一个短SQL事务，提交成功才done。
- sources/ToolMessage/prompt/judge用同一实际证据快照，完整原文、章节路径和版本可定位；不把当前文件冒作历史引用。不信任模型HTML、文件路径或任意URL。
- 100题，A_policy/B_model/C_colloquial/D_missing/E_multi各20；calibration每桶4题、test每桶16题，同事实族不跨split。检索指标在生成门槛和首尾重排前算；拒答/无断言Faithfulness=N/A。
- feedback仅浏览器采集，一次点亮、显示“已反馈”、两按钮锁定，刷新恢复；不发网络、不写user_feedback池。聊天页Vibe Coding，无brainstorm/TDD/code review；后端照常。
- 同主机所有worker/CLI共享解析后的目录与配置身份；线上可与建库同时持库；不新增多机器分布式系统、不迁移到React/Redis/LangGraph/Langfuse。
- 先Context7后具体库实现；版本不匹配转官方并记来源，不事后补查假称先查。沿用环境，不自动重建Python环境、不探测代理；真实模型评估按本章需求执行并保留失败证据。
- 直接本地main，不建分支/worktree/PR，不自动推送；不删除Lite文件、现有数据、审计或Docker卷，绝不down -v。只提交本任务文件，不提交.env/密钥/数据库/运行产物。
- 每任务结束、review结论和finish立即追记dev-notes/ch04.md四项；纯Prompt/数据的测试先行替换为标注样例验证，其余后端RED→GREEN。未实测项不得判通过。

## Review Focus

1. **SQL已提交而文件checkpoint/活动清单尚未发布时进程被kill**：恢复不得重复创建chunk/增加seen_count，也不能提前切换语料；任务3/6/8/13/16分别测试这些断点。
2. **另一worker正在生成/准备审核，第三进程启动或发seal**：不能误标interrupted、提前释放租约或全局关闭他人的线程；任务2/9/11/16实际跨进程复现。
3. **连字符型号、否定条件、未知分类、LIKE特殊字符及陈旧8元来源**：不丢字面约束、不拼接过滤表达式、不放宽分类、不重新激活旧规则；任务3/4/5/6覆盖。
4. **10条完整证据超预算、同一[n]出现在不同回答、旧来源文件被改**：只显示本轮真正送入模型的原文与路径，保留历史快照，越权404；任务7/9/10测试，任务15真实浏览器验收。
5. **模型长时间等待/畸形JSON/越界引用、裁判故障、全部拒答或浏览器重复反馈**：不泄漏未验答案、不把服务故障入池、不伪报忠实度满分/完整评估，反馈不联网且锁定；任务5/7/9/13/14测试，任务15浏览器验收。

---

## 文件职责、顺序与共享接口

这是一个端到端功能：检索结果、实际证据、聊天持久化与评估共用身份，不拆成能各自声明完成的独立项目。任务可独立验证，功能完成仍须走全部验收。推荐Native顺序执行1→18；作者实施后做一次完整后端独立审查，前端按既定例外。执行方式等待用户选择，不先启动实现代理。

| 文件（新/改） | 单一职责 / 任务 |
| --- | --- |
| 改compose.yaml/compose.test.yaml、config.py、.env.example、pyproject.toml；新db/init/04-rag.sql、knowledge/rag_models.py、scripts/migrate_rag.py | Standalone/隔离资源、配置、两表精确迁移 / 1 |
| 新retailq/coordination.py | 本机跨进程租约、配置身份与短锁 / 2 |
| 新knowledge/corpus.py、corpus_import.py；改repository.py；改examples/knowledge/*、db/init/02-seed.sql | 版本化有效语料、可恢复导入与数据扩充 / 3 |
| 新knowledge/retrieval_contracts.py、milvus_store.py | 检索DTO/原生BM25+dense集合与三种原始召回 / 4 |
| 新knowledge/query.py、rerank.py、retrieval.py；新prompts/query_normalize.txt、examples/knowledge/synonyms.json | 保真归一、检索侧扩展、指定模型重排、共享四策略 / 5 |
| 新knowledge/indexing.py；改service.py、review_application.py、review_queries.py | 重建/发布/增量向量化、活动范围和旧入口统一 / 6 |
| 新knowledge/answer.py、rag_gateway.py；新prompts/rag_answer.txt、rag_confidence.json；改customer_service.txt | 预算、首尾证据、结构化自评、引用和门槛 / 7 |
| 新knowledge/rag_repository.py、faith_repository.py、faith_store.py、scripts/faith_cases.py | 原子拒答入池、台账事务与运行回执/处置 / 8 |
| 改services/chat.py、repositories.py、tools/business.py、runtime.py、schemas.py | 原话传递、一次工具执行、RAG发送、会话租约 / 9 |
| 新api/sources.py、api/source_schemas.py；改app.py | 仅会话所有者可读的证据与来源章节定位 / 10 |
| 改knowledge/runtime.py、review_store.py、review_jobs.py、scheduler.py、ownership.py、mining.py、scripts/knowledge.py、app.py及旧fixtures/锁文件；删除旧vectors.py产品适配器 | 同机多worker、跨入口租约/恢复、移除Lite在线依赖 / 11 |
| 新evals/ch04_cases.jsonl、ch04_prompt_checks.jsonl、ch04_annotation.md | ground-truth与难度/桶/split、Prompt已知正负样例 / 12 |
| 新knowledge/evaluation/{contracts,metrics,store,judge,runner,report}.py、scripts/evaluate_rag.py | 公平四策略评估、裁判、恢复、可复核报告 / 13 |
| 新scripts/calibrate_rag.py、docs/ch04-calibration.md | 标注验证、真实阈值冻结与四策略实测 / 14 |
| 改web/app.js、styles.css、index.html | 引用面板、章节定位、反馈锁定 / 15 |
| 新tests/support/ch04_worker.py、ch04_console_server.py、scripts/verify_rag_concurrency.py、smoke_rag.py | 实际worker/kill/聊天验收证据 / 16 |
| 新docs/superpowers/reviews/2026-10-29-ch04-backend-review.md | 实施后独立后端审查和处置 / 17 |
| 新docs/ch04-hybrid-rag.md；改README.md、dev-notes/ch04.md | 可执行交付命令、证据与finish边界 / 18 |

全新Python生产文件均在retailq/；模型/嵌入替身与受控进程只放tests/support/，无生产测试分支。测试文件名逐任务给出，未创建的接口不提前接入线上。

共享合同（执行者先读此处及相关任务Interfaces；内部名字保持一致）：

- MySQL内部ID=int且1..2^63-1；新增来源HTTP/SSE DTO用规范十进制字符串传ID，避免JS精度损失。现有工具items.id兼容原int，sources中的chunk_id/conversation_id用str。run/build IDs为32位小写hex，时间ISO8601/+08:00。
- 任务2：`Coordinator(root: Path, identity: RuntimeIdentity)`；`try_acquire(scope: str, key: str='') -> ProcessLease | None`，`short_lock(scope: str) -> ContextManager[None]`。`ProcessLease.close()`幂等；作用域固定maintenance/audit/conversation/scheduler/evaluation；文件名由合法scope+key摘要组成，不使用原话/用户名当路径。长租约→短锁→SQL短事务，短锁不等长租约；同一线程可重入短锁，同进程不同线程/异进程均互斥。产品工厂`Coordinator.for_settings(settings)`把root固定到项目绝对路径下`artifacts/runtime/coordination`，按MySQL端点/库名生成作用域并绑定实际knowledge目录/Milvus身份；CLI --data-dir不能改变这个协调根目录或对同一数据库另开绑定。测试通过构造器/依赖注入提供隔离root，不在生产加测试分支。
- 任务3：`CorpusStore(root, coordinator).active() -> CorpusSnapshot`、`stage(plan: CorpusPlan) -> str`、`publish(build_id: str, *, expected_generation: int) -> CorpusSnapshot`。不可变snapshot包含generation、collection、identity_hash、source key→digest/IDs、approved_ids、retired_ids、id→fingerprint、corpus_hash；`is_active(record, snapshot) -> bool`是唯一资格判断。无清单时只有诊断/构建可用，线上不猜活动版本。
- 任务4：`Strategy=Literal['dense','bm25','hybrid','hybrid_rerank']`。`SearchFilters(category=None, category_prefix=None, content_type=None)`严格不可变，category与prefix互斥。`IndexedChunk(id, vector, fingerprint, text, category, content_type, source_key, source_version)`；`RawHit(id, score, fingerprint)`。
- 任务4：`RetrievalRequest(raw_question: str, filters: SearchFilters=SearchFilters(), strategy: Strategy='hybrid_rerank', understood: QueryUnderstanding | None=None)`；`QueryUnderstanding(raw_question, normalized_query, bm25_query, rules_hash, fallback_reason)`；`EvidenceChunk(chunk_id, question, answer, category, section_path, source_key, source_version, fingerprint, relevance_rank, score)`；`RetrievalResult(request, understood, snapshot, candidates, hits, diagnostics)`，candidates为阶段排名，hits最多10条。dataclass/Pydantic选择沿用已有模式，但集合字段用tuple/只读结构，不把frozen加mutable dict称深不可变。
- 任务5：`RetrievalEngine.retrieve(request: RetrievalRequest, *, deadline: float) -> RetrievalResult`。归一与HTTP在同步线程中完成，deadline=monotonic绝对时间；各阶段共享剩余预算，不叠加重试倍数。
- 任务7：`CitationSnapshot(n, chunk_id, section_path, question, answer, fingerprint, source_key, source_version, relevance_rank)`；`EvidenceBundle(citations, messages, input_tokens_estimate, bundle_hash)`。`prepare_bundle(raw_question: str, result: RetrievalResult, settings: Settings) -> EvidenceBundle`；`RAGDecision(useful: bool, reason: str, answer: str, cited_numbers: tuple[int,...])`；`RAGGateway.answer(bundle: EvidenceBundle) -> Awaitable[RAGDecision]`；`ConfidencePolicy`按策略与模型/语料/Prompt/归一hash绑定，数字不填猜测默认值。
- 任务8：`RefusalRecord(raw_question, source: Literal['retrieval_low_conf','self_check'], reason)`；`RAGRepository.finalize(conversation_id, user_id, tool_call_id, answer, refusal: RefusalRecord | None) -> int`，原子写assistant/池并返回assistant message ID；幂等凭本轮工具回执而非题目相似度。
- 任务13：评估只调用任务5的retrieve、任务7的prepare_bundle/answer/策略门槛，不另写四条“近似生产”的检索生成路径。评估对话不建真实客服会话、不往低置信度池塞测试题；默认策略编造才写faith_cases。

## 实施前只读检查（任务1的一部分）

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $ProjectPython -m pip check
& $Docker version
git -c safe.directory=D:/AI-Agent/RetailQ status --short
```

Docker CLI路径已在计划阶段只读定位，但引擎/镜像/现有服务未实测。只启动隔离测试资源，不重置演示库；命令环境错误不算产品RED。保存当前回归基线后再改实现；旧报告397通过不替代此时回归。

## Task 1: Standalone配置、测试资源及原始DDL迁移

**Files:** 上表任务1文件；新tests/test_rag_configuration.py、test_rag_schema.py、tests/support/milvus.py；改tests/conftest.py、tests/support/database.py。

**Interfaces:** `migrate_rag(database: Database, ddl_path: Path) -> None`，LowConfidenceQuestion/FaithCase映射；测试fixture提供19531和测试collection前缀，demo为19530。Settings增加milvus_uri/collection_prefix、knowledge_query_timeout_seconds=30、rerank URL/key/proxy/model/timeout及rag_confidence_path。

- [ ] 写RED：`test_chapter_four_ddl_matches_source`断言`assert copied_sha256 == '8840159feb482e325db6b052e99eb646e1758d7bfe3489d7ab184e535b0df118'`；配置拒绝文件URI/错误scheme；DDL字段/FK/ENUM/JSON/唯一键/默认值精确；已有错结构先整体拒绝；cleanup禁止demo端口和非测试前缀。测试重置先删新增两表再删conversations。
- [ ] 运行`& $ProjectPython -m pytest tests/test_rag_configuration.py tests/test_rag_schema.py -q`，确认行为RED，环境故障另记。
- [ ] Compose pin milvusdb/milvus:v3.0.2、quay.io/coreos/etcd:v3.5.25、minio/minio:RELEASE.2024-05-28T17-19-04Z，独立命名卷/healthcheck/localhost绑定、无固定container_name。官方3.0.2标签Compose仍写3.0.1，显式修正。原样复制DDL、先校验hash再映射/迁移，不改既有两表迁移；增加standalone marker，暂留旧Lite测试依赖。
- [ ] 启动`& $Docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql milvus`，同测试GREEN、pip check、server_version=3.0.2及镜像ID入证据。未取得则未完成；留痕并提交`feat: configure standalone Milvus and chapter four tables`。

## Task 2: 同主机跨进程租约和原子短锁

**Files:** 新retailq/coordination.py、tests/test_coordination.py、tests/support/coordination_worker.py。

**Interfaces:** 产出共享合同中的Coordinator/ProcessLease及for_settings；`RuntimeIdentity(mysql_host, mysql_port, mysql_database, milvus_uri, root)`中root为knowledge目录，不含密钥。canonical协调目录中的数据库绑定清单不因数据目录改变而换路径；同库绑定不符拒绝并解释，不自动重新绑定。测试注入临时Coordinator，避免测试全局绑定真实目录；路径解析和身份每次打开核对，锁文件不删除。

- [ ] 写失败测试：`test_cross_process_maintenance_has_one_owner`断言`assert sum(acquired) == 1`；两个实际Python进程同maintenance只有一个成功；持租约进程kill后另进程成功；audit短锁保护CAS不丢更新；同线程重入、异线程互斥；路径逃逸/身份改变拒绝；同库CLI换data-dir仍被原绑定拒绝；close两次安全。
- [ ] 运行`& $ProjectPython -m pytest tests/test_coordination.py -q`得真实行为RED。
- [ ] 以现有Windows msvcrt锁实现为起点实现上述接口，使用规范路径/摘要key和本进程线程锁补齐OS锁的线程语义；Linux分支沿用flock，不新增锁服务。短锁文件句柄从进入持有到退出，失败释放资源，无删除“解锁”。
- [ ] 同一命令GREEN；实际子进程均join确认退出，不用两个对象代替两个进程。更新留痕并提交`feat: coordinate local workers with scoped leases`。

## Task 3: 活动语料、保真数据扩充和可恢复来源导入

**Files:** 新knowledge/corpus.py、corpus_import.py、tests/test_corpus.py、test_corpus_import.py；改knowledge/repository.py、examples/knowledge/manifest.json及三个现有md；新product-specs.md、returns-policy.md；改db/init/02-seed.sql。

**Interfaces:** 产出CorpusStore/Snapshot/Plan/is_active；`CorpusImporter.prepare(manifest: Path, *, operation_id: str) -> CorpusPlan`、`resume(operation_id: str) -> CorpusPlan`。plan含build_id、上一generation、来源digest、active_ids/retired_ids、指纹、归档路径和chunk原文行范围，尚不发布。

- [ ] 写RED：`test_committed_import_resumes_same_ids`断言`assert resumed.active_ids == committed.active_ids`；SQL提交/阶段文件前后恢复不双入；未publish时active不变；未知旧来源冲突阻断；旧8元done也不active。运行`& $ProjectPython -m pytest tests/test_corpus.py tests/test_corpus_import.py -q`。
- [ ] 实现prepared→sql_committed→indexing→verified→published阶段文件，复用完整同指纹链的SQL幂等确认。恢复核对真实SQL/链，不信阶段标签；归档记录heading/块序/行范围，不模糊匹配来源；未知半链暂停。
- [ ] 数据直接整理：保留旧保温杯等样例，按参考四文档扩充五份活动文档；规格采用二级品类/三级型号，使category能区分“商品规格手册/智能猫砂盆”等；FAQ用真实问句，表格/限制完整，去三种人设品牌，统一演示标识与10元，不存同义词副本。纯数据逐节对照、分块/预算验证、型号/关键条件计数与资料hash替代TDD，不以条数证明语义质量。
- [ ] 精确旧seed指纹匹配才迁移FAQ2并退役旧导入块；自定义8元保留并出冲突清单，历史Message不改。import-faq与approved_ids共用资格规则，非文档知识须有确认来源/审核回执。存储测试GREEN，留痕提交`feat: version the active knowledge corpus and expand sources`。

## Task 4: 原生BM25、dense及RRF适配器

**Files:** 新knowledge/retrieval_contracts.py、milvus_store.py、tests/test_standalone_retrieval.py、test_retrieval_contracts.py；扩充tests/support/milvus.py。

**Interfaces:** 产出共享DTO；`MilvusKnowledgeStore(uri: str, identity: dict, *, collection: str)`；`ensure_collection() -> None`、`upsert(items: list[IndexedChunk]) -> list[int]`、`dense_search(vector, limit, filters, *, timeout_seconds) -> tuple[RawHit,...]`、`bm25_search(text, limit, filters, *, timeout_seconds)`、`hybrid_search(vector, text, filters, *, timeout_seconds)`、`fingerprints(ids) -> dict[int,str]`、`close()`。混合两路固定50、融合50/k=60。旧vectors.py暂留，直到任务11切换所有入口后删除，避免中间提交把旧产品启动破坏。

- [ ] 写真实Standalone失败测试：`test_bm25_hits_literal_model`对MH-LP100断言`assert expected_id in [h.id for h in hits]`，并验证其SQL原文确属此型号；text chinese/BM25 Function→sparse、1024 COSINE、禁动态字段、schema/身份不符拒绝；两独立客户端可同时打开；dense/BM25/hybrid对同一category/content_type过滤且未知类别空；含引号、反斜线、%/_分类安全；upsert只确认实际返回ID。
- [ ] `& $ProjectPython -m pytest tests/test_retrieval_contracts.py tests/test_standalone_retrieval.py -q`得RED。参数验证也涵盖NaN/Inf、错误维度、越界INT64和UTF-8字节上限，不能只依赖服务端拒绝。
- [ ] 先按文末Context7/版本源码确认MilvusClient而非Collection的参数名：schema字段dense/text/sparse等、`FunctionType.BM25`、`analyzer_params={'type':'chinese'}`、`SPARSE_INVERTED_INDEX/BM25`、`AnnSearchRequest(expr=...)`两路过滤、client.hybrid_search的ranker。代码不自行生成稀疏向量、不用Python BM25/RRF替代服务端。
- [ ] 同一测试GREEN；另保留run_analyzer对原始含连字符型号的真实输出和命中chunk，不因docs保留字母数字就判已命中。每次测试独立安全collection，结束只清理本测试创建的集合，正常失败输出保留。留痕并提交`feat: add native Milvus BM25 and hybrid retrieval`。

## Task 5: 单轮理解、指定模型重排与共享四策略引擎

**Files:** 新knowledge/query.py、rerank.py、retrieval.py、prompts/query_normalize.txt、examples/knowledge/synonyms.json；新tests/test_query_understanding.py、test_rerank.py、test_retrieval_engine.py。

**Interfaces:** 消费任务3/4；`QueryNormalizer.understand(raw_question: str, *, deadline: float) -> QueryUnderstanding`；`Reranker.rerank(query: str, candidates: tuple[EvidenceChunk,...], *, deadline: float) -> tuple[EvidenceChunk,...]`；产出共享RetrievalEngine.retrieve。注入同步模型/HTTP依赖，只看当前问题。

- [ ] 写RED：`test_normalization_preserves_model_and_negation`在模型丢条件时断言`assert understood.normalized_query == raw_question`及fallback_reason非空；覆盖型号/48小时/否定/起算点、有限扩展、四策略相同understood/snapshot、剩余deadline、陈旧指纹/retired/非done剔除、RRF不套COSINE门槛、rerank重复/越界index或NaN拒绝。运行`& $ProjectPython -m pytest tests/test_query_understanding.py tests/test_rerank.py tests/test_retrieval_engine.py -q`。
- [ ] 实现dense=嵌入50；bm25不调用嵌入；hybrid按固定RRF50；hybrid_rerank以raw_question精排10。dense用normalized_query、BM25用保真扩展。SQL批量回源/有效性复核后才返回完整证据，诊断保留阶段排名。
- [ ] `/v1/rerank`固定model/query/documents/top_n=10/return_documents=false，按原始index关联输入；独立密钥/代理/timeout、不泄漏日志。各阶段共用绝对deadline，query_faq无重试倍增；模型/嵌入/重排故障不降级，只有归一丢字面约束允许回原问并记诊断。
- [ ] 代码测试GREEN；Prompt/同义词至少12条标注样例核对型号、口语、否定和多条件，实际语义验证在任务14归档，未跑不判通过。留痕提交`feat: share query understanding and BGE reranking across strategies`。

## Task 6: 版本化建库、原子切换和审核增量发布

**Files:** 新knowledge/indexing.py、tests/test_index_publication.py；改knowledge/service.py、repository.py、review_application.py、review_queries.py。

**Interfaces:** `IndexBuilder.build(plan: CorpusPlan, *, stop: Event) -> BuildReport`；`publish(build_id: str) -> CorpusSnapshot`；`retry(build_id: str, *, stop: Event) -> BuildReport`；`publish_approved(ids: tuple[int,...], *, operation_id: str) -> None`。BuildReport字段为build_id/phase/expected/indexed/failed/pending/identity_hash；成功须failed=pending=0且expected=indexed。每个build绑定不可变集合名`<prefix>_<build_id>`；发布返回新的generation。KnowledgeService增加retrieve代理，并保留`search_faq(keyword: str) -> list[dict]`作为同引擎的兼容投影，不再有第二种召回逻辑。

- [ ] 写失败测试：`test_build_does_not_publish_partial_index`断言`assert corpus.active().generation == old.generation`，verified后显式publish才加1；index构建中online固定旧snapshot；SQL提交/Milvus upsert/mark_done/verified/publish前后分别中断后恢复；未知collection或清单缺项拒切换；QA批准只入一次、未批准/retired永不补齐；运行中读取新指针不改变本轮旧snapshot；未知旧来源阻断而非批量删除。`& $ProjectPython -m pytest tests/test_index_publication.py -q`确认RED。
- [ ] 实现build在maintenance租约下索引有效ID、批量向量化/指纹核对、SQL mark_done；读查询不等maintenance。发布在audit短锁下验证expected_generation、完整集合schema/原文指纹/数量并原子切换active指针，之后不删旧集合。
- [ ] 审核成功路径、CLI import-faq/vectorize/reconcile共用is_active；任务11负责入口生命周期接线。状态返回active/history/pending分列，旧done不等于当前已索引。若清单写失败，在审计中保留批准及目标ID，retry先核对SQL再继续，不把已批准知识丢掉或展示成已发布。
- [ ] 测试GREEN，连同既有知识服务/审核应用测试核查旧语义（仍可经旧fixtures运行至任务11），不删失败用例。留痕并提交`feat: publish recoverable corpus generations and indexes`。

## Task 7: 实际证据预算、首尾布局与生成自评

**Files:** 新knowledge/answer.py、rag_gateway.py、prompts/rag_answer.txt、rag_confidence.json、tests/test_rag_answer.py、test_rag_gateway.py；改customer_service.txt。rag_confidence.json仅定义未校准格式，真实阈值在任务14生成。

**Interfaces:** 产出共享EvidenceBundle/RAGDecision/prepare_bundle；`OpenAIRAGGateway(settings).answer(bundle)`为async；`ConfidencePolicy.load(path, *, expected_identity)`、`allows(result) -> bool`；`validate_decision(decision, bundle) -> None`。

- [ ] 写RED：`test_bundle_places_strongest_chunks_at_edges`断言`assert [c.relevance_rank for c in bundle.citations] == [1,3,5,7,9,10,8,6,4,2]`，并测1/2/3条；预算计schema/系统/问题/元数据，只裁最低相关完整chunk后重编号；单条也放不下报input_budget_exceeded。畸形/修复JSON、无引用/角标不等/越界、异常finish/tool call拒绝。运行`& $ProjectPython -m pytest tests/test_rag_answer.py tests/test_rag_gateway.py -q`。
- [ ] prepare_bundle按相关性前缀选完整chunk再首尾布局，bundle_hash绑定实际messages。按已查接口明确with_structured_output(method=settings.structured_output_method, include_raw=True)，检查隐式fallback、raw json.loads与严格RAGDecision一致；不复用“售后结构错误”文案，完整校验前不发token。
- [ ] Policy按策略/模型/语料/Prompt/理解hash绑定；缺失或漂移线上503可解释，纯检索诊断仍可用；只有evaluation校准可显式无数字门槛运行，空召回仍拒答，线上没有绕过开关。其他业务工具保留既有流。
- [ ] Prompt要求全部条件有据、负面承诺/型号不串用，代码GREEN。纯Prompt以任务12/14已标样例验证代替TDD；留痕提交`feat: validate evidence and self-check grounded answers`。

## Task 8: 原子拒答入池与跨轮编造台账

**Files:** 新knowledge/rag_repository.py、faith_repository.py、faith_store.py、scripts/faith_cases.py、tests/test_rag_repository.py、test_faith_repository.py、test_faith_store.py。

**Interfaces:** 产出RAGRepository.finalize；`FaithRepository.record(case: FaithEvaluationCase, *, eval_run_id: str) -> FaithCase`、`resolve(eval_id, status, resolution)`、`list_cases(status, offset, limit)`；`FaithStore.begin(run_id, run_identity)`/`resume(run_id)`/`seal(run_id, reason)`/`complete(run_id)`。FaithEvaluationCase含eval_id/bucket/query/strategy/answer/reason/evidence/judge_model，仅hybrid_rerank入长期台账。

- [ ] 写真实MySQL RED：`test_refusal_finalization_is_atomic_and_idempotent`断言两次finalize返回同message ID且pool_count=1；任一写失败全回滚、新turn可新记、原话完整、身份大小写/外键正确、越权404。台账覆盖同run1次/新run2次、复发清resolution保留resolved_at、空evidence回执、旧数组/NULL、commit/checkpoint间kill、新旧run交错和seal后只读。运行`& $ProjectPython -m pytest tests/test_rag_repository.py tests/test_faith_repository.py tests/test_faith_store.py -q`。
- [ ] finalize持conversation行锁，读取本轮持久tool回执。新事务内flush assistant/可选pool，并在tool JSON写`rag_finalization={assistant_message_id,low_confidence_question_id}`；重复调用先核对真实SQL再返原ID，不依赖文本去重。refusal只接受服务端原话与已确认的两种入口，错误不入池。
- [ ] FaithRepository持evaluation租约并事务行锁，比对citations信封eval_run_id：相同不累加，不同更新快照/计数/复发状态；JSON整体赋值，reason不塞回执，唯一冲突回滚重核而非盲加。FaithStore先持久active run逻辑名额；kill释放实际锁但留逻辑名额，必须resume同run或seal，complete前核对全部SQL回执再释放。
- [ ] 测试GREEN；CLI `list`/`resolve --eval-id A01 --status 已解决 --resolution ...`，说明非空、状态合法且不发模型请求。留痕提交`feat: persist refusals and idempotent faithfulness cases`。

## Task 9: 聊天和工具一次性接入RAG质量门禁

**Files:** 改services/chat.py、repositories.py、tools/business.py、tools/runtime.py、tools/schemas.py；新tests/test_rag_chat.py、test_rag_tool_context.py；适配tests/test_tool_chat.py合同扩展。

**Interfaces:** ToolContext增加raw_question=None，PreparedTurn保留原话/租约；`ChatRepository.retrieve_knowledge(request) -> RetrievalResult`；`query_faq(keyword: str, category=None, category_prefix=None, content_type=None)`。keyword仍严格1..64；新过滤可选且不猜。found/items与原4字段保留，内部携带检索结果供服务处理。

- [ ] 写RED：`test_self_check_refusal_stores_original_question`断言pool.raw_question=body.message、pool.source='self_check'；测keyword被精简、一次检索、低分/空的另一个入口、事务回滚、错误无token/无入池。实际进程同cid忙碌409不追加user，新cid创建后得租约，未完成工单继续持租约。运行`& $ProjectPython -m pytest tests/test_rag_chat.py tests/test_rag_tool_context.py tests/test_tool_chat.py -q`。
- [ ] 注入engine/gateway/policy/RAGRepository，FAQ用30秒共享deadline、工具重试0；FAQ故障不影响其他工具，也不交给普通生成编造答案。既有cid租约在load/start_turn前取得，新cid创建后取得。
- [ ] 固定顺序：选择→检索→门槛/预算编号→准确ToolMessage持久化→Decision校验→finalize提交→sources→token→done。空/低分不调用生成，持久空证据后原子拒答；self_check=false可保留证据复盘但不输出草稿。其他工具旧流保留。
- [ ] 取消/断连后同步线程实际退出才释放runtime读名额/句柄；工单shield结算继续持会话租约，finally/settle明确所有者。测试GREEN，补问候不入池/知识外真实路由替身用例；留痕提交`feat: serve cited RAG answers and durable refusals in chat`。

## Task 10: 会话授权的引用快照与原文定位接口

**Files:** 新api/sources.py、source_schemas.py、tests/test_sources_api.py；改app.py、repositories.py。

**Interfaces:** `GET /api/conversations/{conversation_id}/tool-calls/{tool_call_id}/sources?user_id=...`取快照；`GET .../sources/{n}/document?user_id=...`返source_key/source_version/section_path/text/anchor。内部`load_citation_bundle(cid,user_id,tool_call_id) -> tuple[CitationSnapshot,...]`、`load_source_document(citation) -> SourceDocumentView`；ID十进制str，n=1..10。

- [ ] RED：`test_sources_require_conversation_owner`断言越权404；chunk_id='9007199254740993'往返；同n不同轮不串；穿越/HTML无执行；旧源改后仍归档版本；缺归档404不换当前文件。运行`& $ProjectPython -m pytest tests/test_sources_api.py -q`。
- [ ] 写FastAPI前先Context7解析并查0.159.1路由/DTO/SSE，记来源。先归属校验再解ToolMessage严格bundle；文件只查注册key+digest/hash及行范围，QA仅已批准问答/真实来源类型，不暴露其他用户原会话或任意本机路径。缺记录与损坏分别处理，测试GREEN，留痕提交`feat: expose authorized citation snapshots and source anchors`。

## Task 11: 全部入口迁移与多worker运行协调

**Files:** 改knowledge/runtime.py、review_store.py、review_jobs.py、scheduler.py、ownership.py、mining.py、scripts/knowledge.py、app.py、锁文件及旧知识/审核fixtures/tests；删产品vectors.py；新tests/test_rag_runtime.py、test_review_multiprocess.py、tests/support/review_multiprocess_worker.py。

**Interfaces:** `KnowledgeRuntime.open(settings, database, *, coordinator: Coordinator | None=None)`默认for_settings，测试注入隔离协调器；新增retrieve，保留search_faq投影。maintain支持prepare-corpus/build-index/publish-index及旧命令；scheduler只由leader运行并每次重读共享schedule。

- [ ] 旧fixtures迁往真实测试Standalone/共享Coordinator，lite marker改standalone，VectorItem改由实际draft生成IndexedChunk；不删坏审计/水位/恢复/旧响应/降级断言。写RED：`test_new_worker_does_not_interrupt_live_operation`在A阻塞时启动B并断言operation.phase='running'；测孤儿恢复需锁、跨worker幂等/seal、每日唯一/leader接替、关闭非owner不停止A。运行`& $ProjectPython -m pytest tests/test_rag_runtime.py tests/test_review_multiprocess.py -q`。
- [ ] 目录终身owner改为真实作业维护租约；audit短锁保护admit/CAS/seal命令/active发布，202仍先queued。执行线程安全边界读取持久seal，封存者不拿短锁等待维护锁；进程内workers字典为空不代表无人执行。启动只在得维护租约后恢复，shutdown只关本进程资源。
- [ ] 统一runtime/store/jobs/scheduler/chat与CLI身份；scheduler leader及schedule防每日重复；本轮固定snapshot。缺policy不得阻止runtime/管理初始化，policy仅在FAQ生成入口核验，避免尚未校准就无法建库/校准的死循环。CLI增加prepare-corpus --manifest、build-index/publish-index --build-id、query --text/--strategy/--category、init-db --chapter 4 --scope；status无模型且分active/history。已有init-db默认chapter3兼容，未校准仅检索诊断，在线可建库但写作业冲突409。
- [ ] 删除在线Lite导入后移除requirements.in的milvus-lite，按既有锁工具最小更新锁，其他版本不升级；现有多余包不自动卸。新测试GREEN后完整`& $ProjectPython -m pytest -q`和pip check；rg排查残余引用，仅历史路径/文档可留。留痕提交`feat: run retrieval and knowledge maintenance across local workers`。

## Task 12: 冻结ground-truth与Prompt正负样例（数据验证替代TDD）

**Files:** 新evals/ch04_cases.jsonl、ch04_prompt_checks.jsonl、ch04_annotation.md。

**Interfaces:** EvalCase字段eval_id/bucket/difficulty/query/filters/answerable/required_facts/relevant_groups/forbidden_claims/family_id/split。每必需组含等价source_key/version/section_path/quote，任务13映射冻结chunk IDs，不硬写运行自增ID。

- [ ] 直接制作100题及至少20条Prompt已知正负例，遵守Global桶/难度/split/family规则；D_missing明确缺项，E_multi逐必需事实标组。Prompt检查标useful/claim支持和理由，含正确引用、单句编造、错型号/否定、证据不足、承诺到账、全拒答陷阱。
- [ ] 覆盖相似型号与FD10/30断电、CAM1与内置摄像头、内外套清洗、价保/退货起算、会员运费、未知型号/积分期限。作者逐条核quote/digest/条件/等价组、100/20/80与字段限长；记录证据，不把条数当正确性。任务13再自动检查；纯数据不写镜像RED，留痕提交`data: add graded grounded retrieval evaluation cases`。

## Task 13: 四策略评估器、忠实度裁判、恢复和分桶报告

**Files:** 新knowledge/evaluation/{__init__,contracts,metrics,store,judge,runner,report}.py、scripts/evaluate_rag.py；新tests/test_rag_metrics.py、test_rag_eval_data.py、test_rag_eval_store.py、test_rag_evaluation.py。

**Interfaces:** `load_cases(path: Path, snapshot) -> tuple[EvalCase,...]`；`retrieval_metrics(ranked_ids, relevant_groups, ks=(1,3,5,10)) -> RetrievalMetrics`；async `FaithfulnessJudge.judge(answer, citations) -> JudgeResult`和`EvaluationRunner.run(run, *, split) -> EvaluationReport`；EvaluationStore.open_new(output,identity)/resume(output,expected_identity)/checkpoint(row)/seal(reason)。RunIdentity含code/dirty-source、依赖/服务、语料/样例/规则/Prompt/阈值/模型hash和split；runner的engine注入冻结snapshot provider，不中途读取新的active。

- [ ] 写RED：`test_metrics_use_rank_and_distinct_evidence_groups`以[9,4,7]和[{4},{7}]断言recall[1]=0、recall[3]=1、mrr=0.5；等价组不重复计、缺召回0、D_missing/拒答/零断言N/A、2/3支持度精确；布局不影响指标。公平/恢复测understand一次、相同filters/snapshot/预算、裁判看不到strategy/useful/参考答案、错JSON/超时不算通过、身份漂移/重复策略拒resume、Ctrl+C和SQL回执恢复。
- [ ] 数据验证测100/20/80/5桶/family泄漏、quote/digest/章节、可答性/必需组和ID/难度限制；台账query<=512、judge_model<=64、resolution<=300预检，规范化与入库一致。运行`& $ProjectPython -m pytest tests/test_rag_metrics.py tests/test_rag_eval_data.py tests/test_rag_eval_store.py tests/test_rag_evaluation.py -q`记录RED。
- [ ] 实现先冻结每题understood，再四策略调用同engine/bundle/gateway；保存候选50/最终10、实际证据/门槛/答案/裁判。拒答不调裁判刷分，服务故障无“有效答案”，不调用finalize或建客服会话。裁判返回每条claim原句/supported或unsupported/编号或缺证原因，分数由代码计数；显式judge_model默认OPENAI_MODEL独立调用并标同模型偏差。
- [ ] 原子checkpoint分understanding/retrieval/generation/judging/ledger阶段；持久前失去云端响应可能重付费，不能称HTTP exactly-once。FaithStore保护真实轮次，完整expected=len(split_cases)*4（calibration80、test320），且回执全核对才complete；seal后报告只读。
- [ ] report.json/report.md/manifest含总体、桶、难度、split、Recall/MRR/候选50/全证据覆盖、Faithfulness宏微/真实分母、作答拒答/引用/裁判覆盖/故障/延迟，零分母N/A。CLI run --split calibration|test --mode real --output、resume/report/seal；仅artifacts新目录，manifest先保存。退出0完整、1运行/已定义质量失败、2输入/中断/未审阅，完整与质量分开。测试GREEN，留痕提交`feat: evaluate grounded answers with resumable four-strategy reports`。

## Task 14: 标注Prompt验证、真实校准及80题四策略报告

**Files:** 新scripts/calibrate_rag.py、docs/ch04-calibration.md、tests/test_rag_calibration.py；真实输出仅artifacts/evals/ch04/<run_id>/。

**Interfaces:** `fit_confidence_policy(calibration: EvaluationReport) -> ConfidencePolicy`；`validate_prompt_samples(cases, normalizer, rag_gateway, judge) -> PromptCheckReport`；CLI --report/--output发布policy，默认读取路径artifacts/knowledge/rag-confidence.json。

- [ ] 拟合代码RED→GREEN：`test_fit_policy_does_not_share_score_scales`断言`assert set(policy.thresholds) == {'dense','bm25','hybrid','hybrid_rerank'}`；充分0.3/0.9、不充分0.1/0.2时`assert policy.thresholds['dense'] == 0.3`。仅完整20题calibration、每桶4题且正负类都有才拟合；test/应有却缺失的裁判拒绝，合法拒答的N/A不是裁判缺失。候选取观测分数和全拒边界，标签为`case.answerable and all(实际bundle覆盖每个必需组)`，D_missing必须False而非空集all=True；最大化正负类平衡准确率，同分先保留更多充分证据再取较低门槛，不称概率。运行`& $ProjectPython -m pytest tests/test_rag_calibration.py -q`。
- [ ] 真实跑20个已标Prompt正负例（替代Prompt TDD），保留输出/expected/作者判断，确认型号否定保真、自评不足拒答、已知编造能识别。未过先回calibration修Prompt，保留旧失败；不向长期台账写故意伪造样例。
- [ ] 真实20题×4 calibration用同冻结语料/理解、当前嵌入/指定reranker/生成/裁判，无数字门槛模式标provisional且空召回仍拒答。故障保留、不探新代理、不用mock补数；拟合后冻结policy/模型/规则/语料，再原子发布policy，身份漂移需重校准并写明运营约束。
- [ ] 真实80题×4 test，独立目录记录320组策略结果和实际用量；可答才生成/裁判，不预造调用/费用数。默认策略真实编造入台账，所有失败/N/A保留。作者读桶差异，未优于baseline如实说明；不得用test调参，若已看test后重新声称泛化提升需新保留集。留痕实际路径/hash/未验证项，提交校准器与验证后的Prompt/数据修正`feat: calibrate retrieval confidence against labeled examples`，不提交运行产物。

## Task 15: 聊天页引用与一次性满意度反馈（Vibe Coding）

**Files:** 改web/app.js、styles.css、index.html；证据仅artifacts/verification/ch04/browser-<run_id>/。无前端brainstorm/TDD/code review。

**Interfaces:** message新增citations/toolCallId/conversationId/feedback；feedback=null或{value:'up'|'down',createdAt,messageId,turnId,conversationId}，复用sessionStorage并验证恢复数据，不进入模型history。

- [ ] 直接改DOM：只把本轮有效[n]渲染成按钮，面板展示完整chunk/章节及任务10归档定位，轮次不共享映射；支持关闭/Esc/焦点/窄屏。
- [ ] 已完成assistant左下角👍/👎第一次同步持久化、点亮/“已反馈”，禁用两个按钮，重复事件不改选项；未完成/error不作成功反馈。无POST/sendBeacon/fetch反馈入口。
- [ ] 先读computer-use技能后真实浏览器验收：两轮[1]、原文/HTML安全、源改后旧快照、刷新、👍/👎/双击/改选锁定、窄屏/键盘；Network无反馈请求、console无新增错。按实际效果直接迭代，留截图/四项笔记并提交`feat: show source citations and lock local answer feedback`。

## Task 16: 实际多进程故障与端到端验收证据

**Files:** 新tests/support/ch04_worker.py、ch04_console_server.py、scripts/verify_rag_concurrency.py、smoke_rag.py、tests/test_rag_concurrency.py；适配旧verify_knowledge_review_recovery.py而不减场景。

**Interfaces:** `verify_rag_concurrency --mode deterministic|real --output <new-dir>`；`smoke_rag --base-url ... --output <new-dir>`。worker只在tests/support，以ready/phase协议控制断点；记录/join本次owned PID，生产无mock分支。

- [ ] harness先RED→GREEN：`test_harness_rejects_unreached_kill_phase`断言未到指定断点returncode=1且保留原因；范围错误不重置demo/清19530，未退出不算已kill。运行`& $ProjectPython -m pytest tests/test_rag_concurrency.py -q`。
- [ ] 实际两个workers共享root验证同会话409一次写/不同会话并行、建新库与旧库查询并行、切换后新请求新generation、双Milvus客户端；FakeEmbedding只作deterministic协议证据。kill覆盖来源SQL commit、upsert/mark_done、active发布、台账commit/checkpoint；跨worker启动/封存/leader接替/非owner关闭不破坏任务。旧ch03全断点保留。
- [ ] 真聊天smoke确认MH-LP100 BM25命中、不承诺到账、积分有效期明确拒答且SQL原话/会话/入口/原因/时间正确、引用点击可定位；必须经过实际工具选择。live与deterministic分档，HTTP200不作内容正确结论，保留SQL/来源/浏览器证据；留痕提交`test: verify concurrent retrieval and durable RAG recovery`。

## Task 17: 完整后端独立代码审查及必要修复

**Files:** 新docs/superpowers/reviews/2026-10-29-ch04-backend-review.md、修复文件/回归及dev-notes。

**Interfaces:** 固定审查7dfde94→实现HEAD，按已批准spec/plan/实际diff；聊天页3文件排除code review，来源/SSE/访问控制仍审。

- [ ] 实现后读requesting-code-review技能，依已选Native/子代理方式派独立后端审查，记reviewer/基点/分级/文件证据；不把作者计划自审说成独立审查。
- [ ] Critical/Important逐项复现→失败测试RED→最小修复GREEN；Prompt用标注样例、前端用浏览器，库行为先Context7。接受/驳回/延期理由及结果当场记review/笔记，Minor延期明列；重要未修不finish，不为review自行换固定技术。限定文件提交，生产修复重验相关live场景后进入最终验证。

## Task 18: 最终验证、演示命令和本地交付

**Files:** 新docs/ch04-hybrid-rag.md；改README.md、dev-notes/ch04.md；运行manifest仅在artifacts。

**Interfaces:** 交付命令使用任务11/13/14的实际接口；输出JSON提供build_id，避免手填来源身份。以下命令实施时验证后再写入交付指南，目前不代表已执行。

- [ ] 用verification-before-completion运行当前完整`& $ProjectPython -m pytest -q`、pip check、git diff --check，计数读本次输出。核对DDL原hash、服务版本、真实报告、浏览器/并发证据及未触及demo卷/旧Lite；新增修复后重跑相应受影响验证。
- [ ] 验收映射：四策略→14；型号BM25→4/16；引用原文/章节→10/15/16；拒答/池SQL→9/16；多worker/建库→11/16；反馈→15。未取得的live证据明确未验证，不称全功能完成。
- [ ] 验证并交付以下顺序（CLI的JSON输出无额外日志混入stdout）：

```powershell
& $Docker compose up -d --wait mysql milvus
& $ProjectPython -m scripts.knowledge init-db --chapter 4 --scope demo
$CorpusDraft = (& $ProjectPython -m scripts.knowledge prepare-corpus --manifest examples/knowledge/manifest.json) | ConvertFrom-Json
& $ProjectPython -m scripts.knowledge build-index --build-id $CorpusDraft.build_id
& $ProjectPython -m scripts.knowledge publish-index --build-id $CorpusDraft.build_id
& $ProjectPython -m scripts.knowledge query --text 'MH-LP100的适用体重是多少？' --strategy bm25
$RunStamp = Get-Date -Format yyyyMMdd-HHmmss
$CalibrationDir = "artifacts/evals/ch04/calibration-$RunStamp"
$TestDir = "artifacts/evals/ch04/test-$RunStamp"
& $ProjectPython -m scripts.evaluate_rag run --split calibration --mode real --output $CalibrationDir
& $ProjectPython -m scripts.calibrate_rag --report "$CalibrationDir/report.json" --output artifacts/knowledge/rag-confidence.json
& $ProjectPython -m scripts.evaluate_rag run --split test --mode real --output $TestDir
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 2
# 服务运行后在另一个终端执行smoke_rag，使用全新artifacts输出目录
```

- [ ] 指南补scope/变量、旧库冲突、policy漂移、resume/seal、faith处置、反馈本地、历史来源缺失与保留式恢复。输出报告/清单路径、SHA256/长度；限定暂存文件，finish立即记四项后本地提交不推送。最终回复交演示命令、测试/报告数字、未验证或延期项和dev-notes，全部达到才称完成。

## 文档查询证据与执行时再次核验点

本计划阶段已先Context7解析并查询：

1. `/milvus-io/pymilvus`：原生BM25 Function、AnnSearchRequest/RRF及同系列兼容；再核对官方[Milvus3.0.2发布对照](https://github.com/milvus-io/milvus/releases/tag/v3.0.2)、[PyMilvus3.0.2发布](https://github.com/milvus-io/pymilvus/releases/tag/v3.0.2)、[3.0.2标签Compose](https://raw.githubusercontent.com/milvus-io/milvus/v3.0.2/deployments/docker/standalone/docker-compose.yml)。该Compose的server image还是3.0.1，任务1明确修正，不盲复制tag名当内容。3.0.x网页文档当前读取失败，实施前查版本源码/可读官方API，不用2.5文本假称已核对3.0接口。
2. `/websites/sqlalchemy_en`：事务/行锁/MySQL JSON与upsert；Context7返回2.0，版本不足，明确转官方[2.1 Session](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)、[2.1 MySQL dialect](https://docs.sqlalchemy.org/en/21/dialects/mysql.html)。锁行/事务不跨模型等待，JSON整体赋值；flush不是commit。
3. `/websites/reference_langchain`：ChatOpenAI.with_structured_output的method/include_raw/strict与ainvoke；返回基类和派生类不同默认值，实施必须明确method并检查无隐式fallback，沿用项目raw JSON二次校验。

前序已查SiliconFlow指定重排[官方接口](https://docs.siliconflow.cn/docs/api/rerank-post)与Milvus内置chinese/BM25；本计划未调用模型或启动Docker。每个实施任务开始时检查此证据是否覆盖其具体API和安装版本，缺的先Context7再实现；FastAPI新增来源/响应接口的具体文档查询明确放在任务10实现前，不虚构已完成。

## 作者自审与执行交接

计划作者按writing-plans的spec覆盖/步骤粒度/类型一致/Review Focus/比例五项自审，结论与逐项映射写入`docs/superpowers/reviews/2026-10-29-ch04-plan-self-review.md`；这是作者自审，不是独立代码review。

待用户审阅计划并选择Native或逐任务子代理执行。建议Native：18项共享语料身份、租约、证据和事务回执，顺序实施可减少接口重复解释与中间状态漂移；最后仍做独立完整后端审查。收到计划确认后才读取相应执行技能进入实现，不在本文写“任务已完成”。
