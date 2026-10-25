# RetailQ 第 3 章知识库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用硅基流动 BGE-M3 和 Milvus Lite 升级 query_faq，交付结构感知建库、历史对话挖知识、可恢复双写和每日任务。

**Architecture:** MySQL knowledge_chunks 是原文权威源，Lite 的 knowledge 集合仅存显式主键、1024 维 dense 向量与内容指纹。同步、短事务的知识模块由现有单 worker 应用在工作线程中调用，在线查询和每日作业共享一个 Lite 实例；单次 Lite 操作串行，外部模型等待不占数据库事务或 Lite 锁。手动维护 CLI 独占同一知识目录，在线服务运行时不得同时打开文件库。

**Tech Stack:** Windows、现有 Python 3.12.13 `.venv`、FastAPI 0.159.1、SQLAlchemy 2.1.6 / MySQL 8.4、LangChain core 1.6.13 / openai 1.7.2、HTTPX 0.28.1；拟固定 PyMilvus 3.0.2、Milvus Lite 3.2.2、tokenizers 0.22.1（仅分词，不加载嵌入模型）。

**Spec:** `docs/superpowers/specs/2026-10-25-ch03-knowledge-design.md`（2026-10-25用户原话“符合预期，选择native执行方式”，本计划已获准按Native执行）。

## Global Constraints

- 嵌入模型固定硅基流动 `BAAI/bge-m3`；不训练、不微调，不自动切换 Pro 或其他模型，不安装 PyTorch/CUDA/FlagEmbedding。
- Milvus Lite 本地文件模式，默认 `artifacts/knowledge/knowledge.db`，集合固定 `knowledge`、INT64 id、auto_id=false、FLOAT_VECTOR 1024、COSINE；仅 dense 单路。
- 原始 DDL `C:/Users/Administrator/Desktop/ch03-ddl.sql` 两表原样保留；MySQL 为原文权威源，不增表/列，不删卷重建。主键仅允许 1..2^63-1。
- 向量文本固定 `分类：{category}\n问题：{questions}\n答案：{answer}`；section_path/content_type/is_key_clause/prev/next 不进入向量。
- `query_faq(keyword: str)` 保留严格字符串 1–64 字符契约；返回仍是 found/items，item 仅 id/question/answer/category；默认 Top-K 3，answer 最多 1200 字符且保持完整句或结构单元。
- 切分软目标 600 字符、重叠目标 100 字符；不跨章节，不留半句；表格完整行切分并复制表头。不可分单元超过嵌入安全长度时报源位置错误，不静默截断。
- 硅基流动 BGE-M3 每条最多 8192 tokens，不传 dimensions。文档和在线问题统一嵌入服务、模板/归一化版本；配置不兼容明确失败，不混写向量空间。
- 默认每日 03:00 Asia/Shanghai，仅挖“已结束”且有完整最终回复的会话；本地文件库单进程单维护作业，CLI 停机维护，强杀后依靠原文/暂存状态重跑，不宣称分布式恰好一次。
- 代码 TDD；纯 Prompt/数据以标注集及真实模型评估代替 TDD。库/API 先 Context7，失败才用官方文档并说明；技术选型走不通停下问用户。
- 每个阶段当场追记 dev-notes/ch03.md 四项：用户关键原话、产物/验证、拒绝纠偏、失败返工；失败报告保留。本计划的预期结果不是执行证据。
- 直接 main、限定文件提交，不创建分支/worktree/PR，不自动推送。.env、密钥、知识库目录、临时环境和报告不入 Git。

## Review Focus

1. Windows extra 未装 Lite 或二进制依赖缺失：明确失败，不能把 SDK 安装成功当成本地库可用；同目录二进程争用和取消后后台线程仍运行时不得关闭数据库（任务 4、8）。
2. 超长单句、表头比正文还长、转义竖线、代码围栏伪标题以及全部内容是重叠：不丢正文、不制造半句/死循环、报准确来源（任务 2）。
3. Milvus 已成功而 MySQL 未回填、done 向量丢失、MySQL 默认 ai_ci 误判文本相同、重复文档内部重复段：原 ID 恢复、不重复新增、不错误合并、不返回孤儿向量（任务 1、5）。
4. 不同抽取批次的同义问法、相似但适用条件不同的答案、用户猜测/注入/随机订单信息：全局处理但仅保留有客服证据的通用知识；失败不能假称完成（任务 6、7、10）。
5. API 返回乱序/缺项/重复 index、NaN/零向量、401/429/慢响应、无关问题的高分候选：不把故障当空结果，不双层放大重试，按原工具契约可靠拒答（任务 3、5、9、10）。

---

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `db/init/03-knowledge.sql`, `scripts/migrate_knowledge.py` | 原 DDL 副本与显式、可重跑的旧卷迁移；不重放种子 |
| `retailq/knowledge/contracts.py`, `models.py`, `repository.py` | 不可变 DTO/协议、原 DDL 映射、原文/暂存短事务及幂等 |
| `retailq/knowledge/text.py`, `token_budget.py`, `markdown.py` | 唯一向量模板/指纹、固定分词器计数、结构感知切分 |
| `retailq/knowledge/embedding.py`, `vectors.py`, `ownership.py` | 硅基流动 HTTP、Lite 数据访问及元信息校验、OS 目录锁 |
| `retailq/knowledge/service.py` | 原文导入、双写、reconcile、按分数取 MySQL 原文 |
| `retailq/knowledge/qa_gateway.py`, `extraction.py`, `dedup.py`, `mining.py` | 无工具结构化输出、来源核验、全局语义候选/去重、可恢复运行清单 |
| `retailq/knowledge/runtime.py`, `scheduler.py`, `scripts/knowledge.py` | 资源组装/关闭、每日任务、停机 CLI |
| `retailq/config.py`, `.env.example`, runtime/dev 两套 requirements in/lock | 新配置/最小依赖；现有兼容版本不批量升级 |
| `retailq/repositories.py`, `app.py`, `tools/business.py`, `tools/schemas.py`, `tools/runtime.py` | search_faq 委托、单实例注入、语义说明、FAQ 重试边界 |
| `retailq/prompts/qa_extract.txt`, `qa_dedup.txt`, `customer_service.txt` | 抽取/去重边界和更新后的知识回答约束 |
| `examples/knowledge/manifest.json`, `shipping-policy.md`, `product-faq.md`, `after-sales-manual.md`, `conversations.jsonl` | 明确标注的演示文档、真实问法形式和抽取评估输入 |
| `evals/ch03_retrieval.jsonl`, `ch03_mining.jsonl`, `ch03_cases.jsonl` | 检索、抽取/去重、最终聊天的标注集 |
| `scripts/evaluate_knowledge.py`, `smoke_knowledge.py`, `verify_knowledge_recovery.py` | 真实模型评估/演示/跨进程恢复证据 |
| `tests/test_knowledge_*.py`, `tests/support/knowledge.py`, `tests/support/knowledge_worker.py` | 各任务测试及隔离的子进程故障入口 |
| `tests/support/database.py`, `conftest.py`, `test_database.py`, `test_repositories.py`, `test_tools.py`, `test_tool_chat.py`, `pyproject.toml` | 六表测试 fixture、旧断言升级与 markers；保留原会话/工单回归 |
| `README.md`, `docs/superpowers/reviews/2026-10-25-ch03-review.md`, `dev-notes/ch03.md` | 演示命令、最终独立代码审查、逐阶段证据 |

所有新 Python 包添加必要的空 `__init__.py`，不另做重构。本文的文件路径以仓库为根；执行目录是 `D:/AI-Agent/RetailQ`。

## 依赖、资料与运行约定

本章是一条交付链，抽取最终复用同一知识存储/双写，不拆成独立项目。任务 1→2→3→4→5→6→7→8→9→10；任务允许局部 mock 验证自己的接口，但最终必须换成真实 MySQL/Lite/BGE API。

本轮 Context7 对 SQLAlchemy、LangChain、FastAPI 的 resolve 均返回 `Monthly quota exceeded`；前轮 PyMilvus 同样失败。已切换以下官方依据，执行时核对锁定版本，接口缺口不能用记忆补：

- SQLAlchemy 2.1 Session/事务：https://docs.sqlalchemy.org/en/21/orm/session_basics.html；本机 2.1.6 已实查。
- LangChain：https://docs.langchain.com/oss/python/integrations/chat/openai；本机 1.7.2 的 with_structured_output 签名已实查，支持显式 method/include_raw。首次 reference 页面读取因 markdown content-type 失败，已换官方集成页。
- FastAPI lifespan：https://fastapi.tiangolo.com/advanced/events/；HTTPX timeout：https://www.python-httpx.org/advanced/timeouts/。
- Lite 当前 README：https://github.com/milvus-io/milvus-lite；PyPI https://pypi.org/project/milvus-lite/ 和 https://pypi.org/project/pymilvus/ 分别列 3.2.2/3.0.2。PyMilvus master pyproject 的 milvus_lite extra 仍排除 win32；因此 Windows 显式安装两个包，不只依赖 extra。
- 硅基流动：https://api-docs.siliconflow.cn/docs/api/embeddings-post；返回 index 对齐、8192 tokens、float 编码和错误分类按此核验。
- BAAI 模型元数据：https://huggingface.co/api/models/BAAI/bge-m3；分词器固定 revision `5617a9f61b028005a4858fdac845db406aefb181` 的 tokenizer.json，下载时记录 SHA256。
- tokenizers 0.22.1 的官方接口：https://raw.githubusercontent.com/huggingface/tokenizers/v0.22.1/bindings/python/py_src/tokenizers/__init__.pyi，核对 from_file/encode/no_padding/no_truncation。当前无版本首页有未生成 API 提示，不据其推测旧稳定包接口。

PowerShell 定义 `$ProjectPython = '.\.venv\Scripts\python.exe'`；Docker 可用绝对路径 `$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"`。沿用演示 retailq:3306 与隔离 retailq_test:3307、已有 Compose 项目 `retailq` / `retailq-ch02-test`。首次只读扫描时 Docker Engine 未启动；实施前启动并实测，不能将环境失败当 TDD 的业务红。

新增测试 marker `lite`（真实本地存储、不访问云端）；`mysql` 继续要求隔离测试库；真实云端评估走显式脚本，不让默认单测调用外网/收费服务。全量 `pytest -q` 应实际运行 mysql/lite，用例不可因环境缺失全 skip 后宣称通过。

每个任务最后：将本任务真实结果即时写入 dev-notes，`git diff --check`，限定文件 `git add <本任务文件> dev-notes/ch03.md` 后本地提交。提交消息各任务已给出。开始实施前先提交已批准的 spec/plan/过程文档；本轮不做产品提交，不标完成任何任务步骤。

## Task 1：六表迁移、知识 DTO 与事务存储

**Files:** Create `db/init/03-knowledge.sql`, `scripts/migrate_knowledge.py`, `retailq/knowledge/contracts.py`, `models.py`, `repository.py`, `text.py`, `tests/test_knowledge_repository.py`, `tests/support/knowledge.py`；Modify `tests/support/database.py`, `tests/test_database.py`, `tests/conftest.py`（显式加载knowledge模型）。

**Interfaces:** 所有跨模块 DTO 用 frozen dataclass（LLM 输出校验用 Pydantic，见任务 6）。`ChunkDraft(category:str, questions:str, answer:str, section_path:str|None, content_type:str|None, is_key_clause:bool=False)`；`ChunkRecord(id:int, draft:ChunkDraft, prev_chunk_id:int|None, next_chunk_id:int|None, vector_id:str|None, vectorize_status:Literal['pending','done'])`；`StagedQA(id:int,batch_no:str,source_ref:str|None,question:str,answer:str,status:Literal['extracted','kept','discarded'])`。

`KnowledgeRepository(database:Database)` 产出：`import_chunks(drafts:list[ChunkDraft]) -> list[int]`（一份来源文件一个完整事务/顺序）；`pending(limit:int, after_id:int=0)->list[ChunkRecord]`；`all_chunks()->list[ChunkRecord]`；`get_chunks(ids:list[int])->dict[int,ChunkRecord]`；`mark_done(chunk_id:int,vector_id:str,expected_fingerprint:str)->bool`；`reset_pending(ids:list[int])->int`。任务6再提供stage/staging、任务7再提供apply_dedup，不预写生产stub。后续所需 DTO 先在 contracts 定义：`QAProposal(source_ref:str,question:str,answer:str,evidence:str)`；`DedupGroup(member_ids:tuple[int,...], representative_id:int|None, target_chunk_id:int|None, questions:tuple[str,...])`，代表行/已有 chunk 恰选其一，不允许两者同时非空。text.py 在本任务产出 normalize_text(text:str)->str（NFC、统一换行、去行尾空白，保留大小写/段落/表格）、embedding_text(draft:ChunkDraft)->str（Global Constraints固定模板）、content_fingerprint(draft:ChunkDraft)->str（向量文本SHA256），使repository不依赖后续任务的未定义函数。

- [x] **准备隔离 DDL 数据。** 字节复制用户 DDL 到 03-knowledge.sql 并比对 SHA256；测试fixture在硬校验retailq_test:3307后，按原稿补建缺失测试表以便运行测试，不能提前写产品迁移函数。DDL 不改为 IF NOT EXISTS，不用 Base.metadata.create_all；本任务RED还须包含 `test_migration_is_idempotent_and_resumes_partial_schema`，验证已有表先核对、缺表补建且不重放seed。
- [x] **RED：六表映射与原文事务。** 新增 `test_six_tables_match_supplied_ddl`、`test_import_reuses_identical_sequence_and_ids`、`test_repeated_paragraphs_keep_distinct_positions`、`test_same_text_different_case_or_accent_is_not_equal`、`test_pointer_failure_rolls_back_whole_file`、`test_mark_done_checks_fingerprint`。断言新表 unsigned BIGINT/ENUM/自引用外键及 pending 默认值与原稿一致；重复导入 ID 序列和总数不变，位置不同的重复段不错误复用；mid-flush 失败无部分来源文件入库。测试 fixture 先将新表 prev/next 置 NULL 再 DELETE 子表数据，保留原库名/端口硬门禁。
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_repository.py tests/test_database.py -v`；应因知识映射/行为未实现失败，不把“数据库未启动”当红。
- [x] **GREEN：实现迁移/DTO/映射/短事务。** scripts/migrate_knowledge.py 产出 `migrate(database:Database,ddl_path:Path)->None`：逐表缺失才执行对应原CREATE、已有则核验结构，部分完成可再跑，不依赖MySQL DDL整体回滚。knowledge models 复用现有 Base，在应用加载metadata前显式导入，tests/conftest.py同样显式import knowledge.models使单独运行映射测试也恰为六表；两表无 ORM 自动改表。完整 Draft/位置序列在 Python 精确比较，不能依赖 MySQL ai_ci 等值当作内容去重；所有 SQL 绑定参数。不复用到不同前后链，冲突抛脱敏 ServiceError。先生成所有块 ID，再回填同文件指针；LLM/嵌入调用不能进入 Session。staging重试和apply_dedup分别由任务6/7的RED驱动添加到repository，不在本任务预写无测试的逻辑。
- [x] **验证与提交。** 同命令全过，迁移第二次结构/行数不变；在测试库构造仅第一张新表已存在后再迁移通过。记真实输出，提交 `feat: add authoritative knowledge tables and repository`。

## Task 2：结构感知切分、唯一向量文本和 token 预算

**Files:** Create `retailq/knowledge/token_budget.py`, `markdown.py`, `examples/knowledge/manifest.json`, `shipping-policy.md`, `product-faq.md`, `after-sales-manual.md`, `tests/test_knowledge_markdown.py`；Modify `retailq/knowledge/text.py`（只补边界）、`contracts.py`, `requirements.in`, `requirements.lock`, `requirements-dev.lock`, `.env.example`, `retailq/config.py`。

**Interfaces:** Consumes任务1 text.py的normalize_text/embedding_text/content_fingerprint，不另写第二份模板。Produces：`BGETokenBudget(tokenizer_path:Path,max_tokens:int=8192).count(text:str)->int` / `.validate(text:str)->None`（含特殊 token、禁截断/填充）。`SourceDocument`也放在contracts.py，不从markdown反向导入数据层；其签名为`SourceDocument(key:str,text:str,content_type:Literal['faq','policy','manual'],root_category:str,key_sections:tuple[str,...])`；`load_manifest(path:Path)->list[SourceDocument]`；`chunk_document(document:SourceDocument,budget:BGETokenBudget,*,target_chars:int=600,overlap_chars:int=100)->list[ChunkDraft]`。

section_path 固定为 `<清单相对文件路径>@<全文SHA256> / <标题路径> / 块<顺序号>`，区分来源/文档版本/重复段位置而不增列、不进入向量；超过 VARCHAR(512) 报错，不截短。同版本可幂等、改版作为追加形成独立链，旧版不自动删除。category 仅上级标题路径或 root_category，questions 不含这些身份信息。

- [x] **准备分词与示例格式。** 只新增 `tokenizers==0.22.1`，保留现有兼容锁版本；用 HTTPX 从固定 HF revision 只下载 tokenizer.json 到 artifacts/knowledge/assets，记录 SHA256，不下载权重。清单示例给出 `{"files":[{"path":"shipping-policy.md","content_type":"policy","root_category":"演示配送","key_sections":["配送规则/运费标准"]}]}`；FAQ 用问句标题，多个问法用标题后的 `问法：...` 行，正文为答案。拒绝清单根目录之外的相对路径。样例明确写演示，且 questions 不手工加入“邮费”来掩盖语义召回。
- [x] **RED：结构与边界。** `test_policy_heading_path_and_faq_real_questions`、`test_fenced_code_is_not_heading`、`test_complete_sentence_overlap_never_loses_new_text`、`test_table_chunks_repeat_header_with_escaped_pipes`、`test_oversized_sentence_or_row_reports_source`、`test_heading_and_header_count_towards_token_limit`、`test_metadata_is_absent_from_embedding_text`、`test_manifest_path_escape_and_field_overflow`。完整句去重拼回覆盖源正文；每表块同表头/分隔行；元数据用唯一哨兵断言不在向量文本。

```python
def test_exact_embedding_template():
    draft = ChunkDraft('配送', '运费标准', '演示：满99元免运费。', 'META_ONLY', 'policy', True)
    assert embedding_text(draft) == '分类：配送\n问题：运费标准\n答案：演示：满99元免运费。'
    assert 'META_ONLY' not in embedding_text(draft)
```

- [x] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_markdown.py -v`，确认缺少切分行为；分词器缺下载/依赖不是有效红。
- [x] **GREEN：实现切分。** 纯 Python 状态机识别 ATX 标题、fence、段落、列表和表格，不引入大文档框架。章节→段落/结构单元→完整句递归，中文 `。！？`/英文终止标点连同后置引号归到前句，小数/URL 内句点不误切；无终止标点的最后一段保留为源文本完整单元。重叠只取上块尾部完整句/完整行，空间不足优先减重叠而不丢新内容。不可拆单元超 8192 token 明确拒绝。分别校验 category<=255、section_path<=512、TEXT utf8 字节<=65535。分词器使用 from_file、no_truncation、no_padding、encode(add_special_tokens=True)，不按字符猜 token。
- [x] **验证与提交。** 同命令通过，并将三份演示文档的切分预览、最大 token、原文覆盖/表头检查输出到 artifacts/knowledge/chunk-preview.json（不是检索效果报告）。任务 1/2 回归通过，提交 `feat: chunk Markdown knowledge with complete boundaries`。

## Task 3：硅基流动 BGE-M3 嵌入适配器

**Files:** Create `retailq/knowledge/embedding.py`, `tests/test_knowledge_embedding.py`；Modify `contracts.py`, `retailq/config.py`, `.env.example`, `requirements.in` / 两份 lock（HTTPX 从 dev-only 升为直接运行依赖，保留 0.28.1）。

**Interfaces:** `EmbeddingProvider` Protocol：`embed(texts:list[str],*,deadline:float|None=None)->list[list[float]]`、`close()->None`；`SiliconFlowEmbedding(settings:Settings,budget:BGETokenBudget,*,transport:httpx.BaseTransport|None=None)` 实现该协议，deadline 是 time.monotonic 的绝对总预算，不是墙上时间。正常输出按输入顺序排列且 L2 归一化。

新增配置：`EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1`、`EMBEDDING_API_KEY` SecretStr、`EMBEDDING_MODEL=BAAI/bge-m3`（Literal）、`EMBEDDING_BATCH_SIZE=16`、`EMBEDDING_TIMEOUT_SECONDS=10`、`EMBEDDING_RETRIES=1`（0..1）、`KNOWLEDGE_TOKENIZER_PATH`。Settings新增is_embedding_configured:bool（只判断必要配置存在，不发请求）。独立于 OPENAI_*；缺 key 不覆盖聊天模型配置，不输出请求 Authorization/上游响应全文。

- [ ] **RED：请求/结果契约。** HTTPX MockTransport 捕获 URL/JSON，`test_request_has_only_bge_parameters` 断言 model/input/encoding_format=float、没有 dimensions、metadata、chat key；`test_reverse_indices_restore_input_order`、`test_missing_duplicate_or_out_of_range_index_fails`、`test_wrong_dimension_nan_inf_zero_and_empty_fail`、`test_retry_budget_and_authentication_no_retry`。401/403/400 不重试，429/502/503/504/临时网络最多一次、Retry-After 不超过剩余 deadline；超过预算不再发请求。空批次返回 []，批内空串/超长串整批在发送前拒绝。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_embedding.py -v`，记录实际失败原因。
- [ ] **GREEN：实现 sync HTTPX 适配器。** 用 Client(timeout=...,trust_env=False) 和 Bearer 头，POST /embeddings；严格校验完整 JSON 结构、model 身份、条数、index 和有限实数（bool 不当 float），校验后统一归一化。HTTPX timeout 是 connect/read/write/pool 分项，不能当作完整逻辑总超时；调用前/重试前及返回后核对 deadline，工具外层仍保留总等待期限。将错误映射 `embedding_unavailable`/`embedding_invalid_response` 等脱敏 ServiceError，绝不返回 [] 伪装 miss。
- [ ] **验证与提交。** 单测通过、pip check 无冲突；在任务 10 才做有凭据的真实调用。记录 mock 只证明适配器行为，提交 `feat: add validated BGE-M3 embedding client`。

## Task 4：Windows Lite 文件库、集合身份与目录独占

**Files:** Create `retailq/knowledge/vectors.py`, `ownership.py`, `tests/test_knowledge_vectors.py`；Modify `contracts.py`, `retailq/config.py`, `.env.example`, requirements in/lock, `pyproject.toml`。

**Interfaces:** `VectorItem(id:int,vector:list[float],fingerprint:str)`；`SearchHit(id:int,score:float,fingerprint:str)`。`DirectoryOwner(root:Path)` context manager 非阻塞独占，同进程重复资源持有也拒绝；不依靠 PID 文件存在与否判断活锁。`MilvusKnowledgeStore(uri:Path,identity:dict[str,str])`：`ensure_collection()->None`、`upsert(items:list[VectorItem])->list[int]`、`search(vector:list[float],limit:int,*,timeout_seconds:float|None=None)->list[SearchHit]`、`fingerprints(ids:list[int])->dict[int,str]`、`close()->None`。所有Lite SDK调用共享threading.Lock；search的锁获取与SDK调用共用timeout_seconds剩余预算，过期立即失败，不能无限等锁。云端/SQL不持此锁。

- [ ] **准备兼容依赖。** 官方 PyPI 包锁候选为 `pymilvus==3.0.2`、`milvus-lite==3.2.2`，Windows 显式列两项以绕开 extra 对 win32 的忽略，不安装 pymilvus[model]。先在项目 Python3.12做 pip --dry-run / --report，检查 faiss-cpu/pyarrow/numpy/grpcio Windows wheel及现有锁冲突，报告后只加必需依赖；干净临时环境验证后再更新现有 `.venv`。若无可用组合，不编译大栈/换WSL/改数据库，保留错误后问用户。
- [ ] **RED：真实 Lite 行为和所有权。** `test_lite_explicit_schema_and_cosine_order`、`test_upsert_same_primary_key_survives_reopen`、`test_schema_or_identity_mismatch_is_rejected`、`test_signed_int64_range_is_enforced`、`test_second_process_cannot_own_directory`、`test_owner_recovers_after_process_termination`。在临时目录创建 1024 维确定性向量，只用于数据库协议，不算语义效果。二进程测试持真实 OS 锁后终止持有者，下一进程可正常取得，不删除数据文件/伪造 PID。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_vectors.py -v`，缺包/不支持平台先修环境，红必须来自尚未实现的适配器行为。
- [ ] **GREEN：实现所有权与 Lite。** ownership 在 Windows 用 msvcrt.locking，非 Windows 用 fcntl 兼容分支（按 Python 官方 docs 核对），只锁指定知识根目录的 owner.lock，进程退出由 OS 释放。创建显式 id/embedding/fingerprint schema、COSINE FLAT 索引、dynamic=false；校验实际 schema/index。身份 JSON 包含 provider/model/dimension/metric/text-template-version/normalization-version/tokenizer-revision/schema-version，不含密钥；临时文件写后 os.replace 原子落盘。没有身份但已有数据则失败，空新库才创建身份；异常不 drop_collection。
- [ ] **验证与提交。** 同命令全过，记录 Windows 实际包版本与写入→关闭→重开→检索结果；runtime/dev 两锁各在干净临时环境安装和 pip check 成功，不把仅依赖解析当运行成功。提交 `feat: persist dense vectors with Milvus Lite ownership`。

## Task 5：导入、可恢复双写与权威原文检索

**Files:** Create `retailq/knowledge/service.py`, `tests/test_knowledge_service.py`；Modify `repository.py`, `contracts.py`, `tests/support/knowledge.py`。

**Interfaces:** `VectorizeReport(pending_before:int,done:int,failed:int,pending_after:int)`；`KnowledgeService(repository:KnowledgeRepository,embedding:EmbeddingProvider,vectors:MilvusKnowledgeStore,budget:BGETokenBudget,*,top_k:int=3,min_score:float=0.0,batch_size:int=16,query_timeout_seconds:float=5.0)`：`import_documents(documents:list[SourceDocument])->list[int]`、`import_faq()->list[int]`、`vectorize_pending(*,stop:threading.Event|None=None,fault:Callable[[str],None]|None=None)->VectorizeReport`、`reconcile()->list[int]`、`search_faq(keyword:str)->list[dict]`、`status()->dict[str,int]`。repository 增加 `faq_drafts()->list[ChunkDraft]`（原问法，content_type=faq，无指针链；作为单块来源依次导入）。tests/support/knowledge.py提供service:KnowledgeService fixture，使用隔离MySQL、临时Lite及将“配送查询”映射到运费种子的确定性嵌入，不作为语义效果证据。

- [ ] **RED：双写每个中断边界。** `test_pending_is_committed_before_embedding`、`test_crash_after_upsert_reuses_ids`、`test_failed_rows_remain_pending_while_next_rows_progress`、`test_done_missing_vector_reconcile`、`test_search_preserves_score_order_and_uses_mysql_answer`、`test_orphan_pending_stale_vectors_are_never_returned`。真实 MySQL+Lite 配假嵌入确定性向量；断言重跑 chunk 数/ID 不变、每 ID 当前向量一条、pending 清零。故障只改 MySQL 答案而不改向量时，搜索指纹不匹配即跳过，不能返回旧向量支撑的新答案。

```python
def test_faq_contract_has_no_internal_fields(service):
    items = service.search_faq('配送查询')
    assert items
    assert set(items[0]) == {'id', 'question', 'answer', 'category'}
    assert len(items[0]['answer']) <= 1200
```

- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_service.py -v`，测试始终限定 retailq_test:3307 和临时 Lite 目录。
- [ ] **GREEN：实现批处理与恢复。** 每批读取有序 pending 快照→关闭 Session→嵌入→upsert 并核对返回 ID 集→带原内容指纹条件回填 done。失败不在同一次运行原地无限重试，使用 after_id 单调前进并汇总非零失败。仅在批次边界响应 stop；upsert后故障不改 done，重跑同 ID。reconcile 用全部 chunk ID 分页核查 Lite 指纹，缺失/不匹配恢复 pending，绝不重建原文。
- [ ] **GREEN：实现 dense 查询。** 以monotonic创建query_timeout_seconds总预算并传给embedding.embed(deadline=...)，Lite.search接收剩余timeout_seconds；不得给每个重试重新算完整预算。嵌入查询→同一 dense 通道 overfetch=min(top_k*4,100)→相似度阈值过滤→批量 MySQL get→done/vector_id/指纹校验→保持分数顺序最多 top_k。答案<=1200 字符原样返回；更长时按完整句/列表项/表格行裁到上限，表格保留表头且至少一完整数据行，无可用完整单元跳过候选。无候选返回 []，依赖故障透传脱敏异常；不执行 LIKE，不按 ID 重排，不加关键词召回。
- [ ] **验证与提交。** Task1–5回归通过；记录各故障点实际 pending/ID 证据，提交 `feat: recover knowledge dual writes and retrieve authoritative chunks`。

## Task 6：历史对话快照、批量 QA 抽取与暂存

**Files:** Create `retailq/knowledge/qa_gateway.py`, `extraction.py`, `retailq/prompts/qa_extract.txt`, `examples/knowledge/conversations.jsonl`, `evals/ch03_mining.jsonl`, `tests/test_knowledge_extraction.py`；Modify `repository.py`, `contracts.py`。

**Interfaces:** repository在本任务新增 `stage(batch_no:str,proposals:list[QAProposal])->list[StagedQA]` 与 `staging(batch_nos:list[str])->list[StagedQA]`。`MiningMessage(id:int,role:str,content:str|None)`；`ConversationSnapshot(source_ref:str,conversation_id:int,max_message_id:int,messages:tuple[MiningMessage,...])`；repository `closed_conversations()->list[ConversationSnapshot]`（固定截止 ID 的已结束完整历史）。`DedupCandidate(anchor_id:int,staging:tuple[StagedQA,...],existing:tuple[ChunkRecord,...])` 也在contracts定义；`QAGateway.extract(batch:list[ConversationSnapshot])->list[QAProposal]`、`.deduplicate(candidates:list[DedupCandidate])->list[DedupGroup]`（任务7实现）、`.close()->None`；`OpenAIQAGateway(settings:Settings)` 复用现有 OPENAI_* 配置、独立拥有同步 HTTP 客户端。qa_gateway.py定义额外字段禁止的Pydantic模型 `ExtractedItem(source_ref,question,answer,evidence)`、`ExtractionResult(items:list[ExtractedItem])`，成功后映射到QAProposal；任务7再加 `DedupResult(groups:list[DedupGroupModel])`，DedupGroupModel字段/约束与DTO一致。`batch_conversations(snapshots:list[ConversationSnapshot],*,max_conversations:int,input_budget:int)->list[list[ConversationSnapshot]]`；`extract_to_staging(repository:KnowledgeRepository,gateway:QAGateway,batch:list[ConversationSnapshot],batch_no:str)->list[StagedQA]`。

- [ ] **RED：可测的抽取边界。** `test_only_closed_complete_snapshots_are_read`、`test_batch_budget_keeps_whole_turns_and_source_ids`、`test_question_and_evidence_must_belong_to_source`、`test_unknown_source_and_malformed_json_do_not_stage`、`test_repeated_batch_reuses_staging_rows`、`test_extraction_does_not_hold_sql_connection_while_waiting`。source_ref 固定 `conversation:<id>:message:<max_id>`，长会话拆完整轮次时追加片段号；不能把一个用户问句和其完整客服答复拆开。单轮仍超预算则报告/跳过本次 run 的完成标记，不截为半轮。网络/JSON失败不写虚假空抽取成功。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_extraction.py -v`；fake QAGateway 仅校验编排，另以现有可控 HTTP 上游验证真实 LangChain wire 结构。
- [ ] **GREEN：实现无工具结构化输出。** QAProposal 的 evidence 为当前 source 中客服原话连续证据，question 必须属于用户原话，answer 必须等于或是证据中的完整答案单元，不自由生成事实。ChatOpenAI.with_structured_output 显式 method=settings.structured_output_method、include_raw=True、max_retries=0、禁止自动 function_calling 回退；沿用现有严格 raw json.loads/结束标记检查，不让修复半截 JSON 通过。每批结果全部校验后短事务 stage；失败保留 run 未完成状态。DTO返回后无活 Session。
- [ ] **Prompt/数据评估代替 TDD。** 建至少12个标注会话场景（正例、同义跨批、条件差异、用户猜测、注入、随机订单事实、无答复、长会话、空知识），标注应保留 question/证据与应拒原因。本任务先逐条人工核对标注和做可控上游格式验证；有凭据的真实语义评估在任务10统一运行，不能先声称 Prompt 效果通过。
- [ ] **验证与提交。** 测试全过，暂存批次重跑行数不变；记录纯 Prompt 尚待真实评估，提交 `feat: stage grounded QA extraction from closed conversations`。

## Task 7：全局去重与可恢复挖掘流水线

**Files:** Create `retailq/knowledge/dedup.py`, `mining.py`, `tests/test_knowledge_mining.py`；Create `retailq/prompts/qa_dedup.txt`；Modify `qa_gateway.py`, `repository.py`, `contracts.py`。

**Interfaces:** repository在本任务新增 `apply_dedup(groups:list[DedupGroup])->list[int]`（单组事务，返回入库/复用知识ID）。`MiningReport(run_id:str,extracted:int,kept:int,discarded:int,vectorized:int,complete:bool)`；`global_dedup(staging:list[StagedQA],existing:list[ChunkRecord],embedding:EmbeddingProvider,gateway:QAGateway,*,min_score:float=0.85,candidate_k:int=8)->list[DedupGroup]`；`MiningPipeline(repository:KnowledgeRepository,gateway:QAGateway,embedding:EmbeddingProvider,service:KnowledgeService,run_root:Path)`：`run(*,resume_run_id:str|None=None,stop:threading.Event|None=None)->MiningReport`。整个维护调用由任务8的 KnowledgeRuntime 单作业锁包住；测试在 fixture 中同样加锁，不能依赖新增 MySQL 唯一索引。

- [ ] **RED：全局、事务与续跑。** `test_cross_batch_duplicates_are_compared_together`、`test_similar_different_region_or_threshold_is_not_merged`、`test_equivalent_existing_chunk_discards_staging_without_new_row`、`test_group_commit_updates_staging_and_pending_atomically`、`test_resume_after_each_phase_and_missing_run_manifest`、`test_malicious_group_ids_or_answer_rewrite_are_rejected`。至少两抽取批次各含一个重复及一不同适用条件条目，断言非逐批去重；跨中断运行仍只有一份保留知识。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_mining.py -v`；在组事务 commit 前故障断言 kept/discarded/知识行全部回滚。
- [ ] **GREEN：实现全局候选与决策校验。** 规范化完全相同 QA 先精确去重；对本次全部 staging 和已有知识用 BGE dense 向量统一计算分块余弦候选（不将这些辅助向量写入在线 knowledge 集合），每 QA 取相似度>=0.85的最多8个候选，参数只用于离线去重并由任务10校准。LLM 决策分组只接收有明确 source/staging/chunk ID 的有界候选，不能凭相似度自动合并；合并组要求代表与每个成员直接得到“同问题、同适用条件、答案等价”认可，不用传递闭包把A≈B≈C当A≈C。冲突/不确定保留为独立 QA。一次校验所有行恰有归属、无越权ID、questions来自成员真实问法、answer取代表现有证据，不能由模型改写条件。
- [ ] **GREEN：事务落库与运行清单。** 原子 JSON 清单记录 source快照/Prompt哈希/确定batch_no/各阶段/来源上界，先写temp再os.replace；完成批次后再记 checkpoint。run_id 为本次有序来源上界+Prompt版本的 SHA256前32位，batch_no<=64确定生成。每批最多20会话且扣除Prompt/Schema/source包装开销后受settings.input_budget约束；批次未全完成不得进入整体去重；一组知识 pending+staging状态在一个事务写入，category=对话知识、content_type=faq、is_key_clause=false、section_path/prev/next为空，source_ref不进入最终表。所有组落库后 service.vectorize_pending；complete 仅在抽取/去重均完且目标 pending补齐时为true。清单丢失可重扫：stable batch查重/已有chunk去重及全库 pending扫描兜底，不能因清单说done而跳过恢复。
- [ ] **验证与提交。** Task1–7 回归通过。为 Prompt 去重增加条件冲突/跨批样例标注，留到任务10真实评估；提交 `feat: deduplicate staged QA globally with resumable mining`。

## Task 8：单进程资源、每日调度与维护 CLI

**Files:** Create `retailq/knowledge/runtime.py`, `scheduler.py`, `scripts/knowledge.py`, `tests/test_knowledge_runtime.py`, `tests/test_knowledge_cli.py`；Modify `retailq/config.py`, `.env.example`, `tests/conftest.py`。

**Interfaces:** `KnowledgeRuntime.open(settings:Settings,database:Database)->KnowledgeRuntime`；同步 `search_faq(keyword:str)->list[dict]`、`maintain(action:str,options:dict,stop:threading.Event)->dict`（action只能为受控枚举import/vectorize/reconcile/mine/status）、`close()->None`；runtime 拥有 owner→store→embedding→QAGateway 和一个维护 threading.Lock。所有在线/离线调用进入runtime时用Condition登记活动操作，退出finally减计数；在线并发最多2个（BoundedSemaphore非阻塞获取，满额返回knowledge_busy），close先拒绝新调用，再等所有活动线程退出后关闭资源。`next_run_at(now:datetime,*,hour:int=3,minute:int=0)->datetime`；`DailyKnowledgeScheduler(runtime:KnowledgeRuntime,*,clock:Callable[[],datetime])` 的 `start()->None`、`async stop()->None`。调度只把一份受控作业交工作线程，保存Task句柄和stop事件，线程退出前不释放runtime资源。

配置：`KNOWLEDGE_ENABLED=false`（未配key默认不阻断旧服务）、`KNOWLEDGE_SCHEDULE_ENABLED=true`（启用知识后默认启用每日作业）、`KNOWLEDGE_DATA_DIR=artifacts/knowledge`、`KNOWLEDGE_TOP_K=3`（1..20）、`KNOWLEDGE_MIN_SCORE=0.0`（-1..1，任务10校准后记录演示配置）、`KNOWLEDGE_CHUNK_TARGET=600`、`KNOWLEDGE_CHUNK_OVERLAP=100`（0<=overlap<target）、`KNOWLEDGE_SCHEDULE_HOUR=3`/`MINUTE=0`、`KNOWLEDGE_TIMEZONE=Asia/Shanghai`。另增KNOWLEDGE_DEDUP_MIN_SCORE=0.85、KNOWLEDGE_DEDUP_CANDIDATE_K=8供任务7/10统一传参和记录。Settings.is_knowledge_configured仅组合enabled及必要配置是否填写，不检查服务就绪。Windows无系统IANA库时显式添加并锁tzdata，不以宿主时区代替。

- [ ] **RED：调度与关闭顺序。** `test_schedule_uses_shanghai_not_host_timezone`、`test_late_start_runs_once_without_replaying_missed_days`、`test_maintenance_is_single_job_while_reads_remain_bounded`、`test_cancelling_scheduler_waits_for_worker_before_close`、`test_timed_out_faq_thread_keeps_resources_until_done`、`test_stop_between_batches_leaves_recoverable_pending`、`test_disabled_or_missing_config_reports_not_ready`、`test_open_failure_releases_directory_and_clients`。用可控时钟/Event，不真实等到03:00或sleep几十秒；向量锁只包Lite调用，慢云端不阻塞已在内存完成的其他操作。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_runtime.py tests/test_knowledge_cli.py -v`；CLI断言本章单元marker不访问真实云端。
- [ ] **GREEN：实现资源与调度。** 每个实例一个路径owner；`asyncio.to_thread` 执行同步维护，shutdown 设stop并 shield 等待已提交作业结束，不把取消async等待当线程已停。每个阶段核对stop，关闭scheduler后通过工作线程调用runtime.close，等待在线操作计数也归零，随后才能close Session工厂、HTTP或Lite。每日触发串行mine并最终补pending；恢复时当天漏跑只执行一次并写清单，不重放多天。定时失败记录脱敏失败阶段/待补数量，下个周期能继续，状态ready不同于任务成功。
- [ ] **GREEN：实现 CLI。** `python -m scripts.knowledge` 支持 `init-db --scope demo|test`（只调用任务1迁移）、`prepare-tokenizer`（只下载固定分词文件）、`import --manifest PATH`、`import-faq`、`vectorize`、`reconcile`、`mine [--resume RUN_ID]`、`status`、`query --text TEXT`；全局 `--data-dir PATH`、`--output PATH`。init-db 的scope硬校验库名/端口；其他维护在有owner冲突时明确退出，绝不强杀服务/删锁；status仅读MySQL时可不打开Lite并标向量未核验，其他调用独占资源。退出码0成功、1执行失败/未全部完成、2缺配置/依赖/目录占用；所有异常脱敏，保留pending不清库。
- [ ] **验证与提交。** 在隔离临时目录用CLI跑 import→重复import→vectorize→status→关闭重开query；用fake云端验证协议，不称真实语义通过。任务1–8回归通过，提交 `feat: schedule resumable knowledge maintenance in one process`。

## Task 9：原客服入口接入语义 query_faq

**Files:** Modify `retailq/repositories.py`, `retailq/app.py`, `retailq/tools/business.py`, `retailq/tools/schemas.py`, `retailq/tools/runtime.py`, `retailq/prompts/customer_service.txt`, `tests/test_repositories.py`, `tests/test_tools.py`, `tests/test_tool_chat.py`, `tests/conftest.py`；Create `tests/test_knowledge_chat.py`。

**Interfaces:** `ChatRepository(database:Database,faq_search:Callable[[str],list[dict]]|None=None)`；`search_faq(keyword:str)->list[dict]` 委托注入，未配置时抛 `knowledge_not_configured` 503，不再查faq表。business_tools/build_registry/ChatService原调用接口不变；app lifespan 创建唯一runtime，把runtime.search_faq注入ChatRepository，启动scheduler并保存关闭顺序。只有选到query_faq才因缺知识配置失败，不影响其他四工具/售后提取。

- [ ] **RED：契约/故障/无回退。** `test_faq_schema_and_result_shape_are_unchanged`、`test_faq_repository_delegates_without_sql_like`、`test_missing_knowledge_fails_only_faq_tool`、`test_embedding_fault_is_not_a_successful_miss`、`test_faq_retry_is_not_multiplied`、`test_lifespan_owns_one_store_and_shutdown_order`。断言原字段/strict/长度仍相同，结果不含score/section_path；记录SQL捕获证明生产search_faq不执行LIKE。FAQ由嵌入适配器管理最多一次重试，ToolRegistry把query_faq注册为retry_safe=false（其他读工具重试不改）；超时后底层有限操作仍由共享store锁保护，不能spawn无界线程或关闭仍使用的资源。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_chat.py tests/test_tools.py tests/test_repositories.py tests/test_tool_chat.py -v`；将旧FAQ LIKE断言替换为委托/语义契约的预期红，不删除原身份/工单/历史/流式回归。
- [ ] **GREEN：组装线上链路。** app仅在settings.is_knowledge_configured时组装runtime，缺配置时保留其他能力且FAQ单独报未配置；ChatRepository其他方法不动；FAQInput.keyword只更新description，保留原schema约束。工具描述移除关键词LIKE限制；客服Prompt说明语义知识依据、miss和服务故障区别，保留仅按返回事实/演示规则/无退款执行能力/简洁中文约束。本轮不改页面布局或新增API后台；health可加knowledge_configured/schedule_enabled但明确仅表示配置，不能当端到端探活。
- [ ] **Prompt/数据评估准备。** 保留 ch02 历史评估文件原样，创建 ch03_cases.jsonl 替代其“邮费必然miss”业务预期；复用 scripts.evaluate_tools 的通用参数/报告能力，以新cases运行，不通过改写旧报告冒充回归。对FAQ语义本轮输出最多TopK的真实结果，最终回答数字/条件必须可追溯。
- [ ] **验证与提交。** 默认完整测试含MySQL/Lite全过，售后提取/其他四工具/取消后工单持久化不回退；纯Prompt真实效果仍等待任务10。提交 `feat: route query_faq through dense semantic knowledge search`。

## Task 10：真实评估、中断演示、独立代码审查与交付

**Files:** Create `scripts/evaluate_knowledge.py`, `smoke_knowledge.py`, `verify_knowledge_recovery.py`, `tests/support/knowledge_worker.py`, `tests/test_knowledge_evaluation.py`, `evals/ch03_retrieval.jsonl`, `docs/superpowers/reviews/2026-10-25-ch03-review.md`；Modify `evals/ch03_mining.jsonl`, `evals/ch03_cases.jsonl`, `scripts/evaluate_tools.py`（仅必要通用能力）、`README.md`, `dev-notes/ch03.md`。

**Interfaces:** `scripts.evaluate_knowledge` 参数 `--mode retrieval|mining --cases PATH --output DIR` / `--review REPORT`；`scripts.smoke_knowledge --base-url URL --output FILE`；`scripts.verify_knowledge_recovery --scope test --output DIR`。评估报告固定全部case ID清单、run_completed、实际输出/检索ID/答案/分数/模型配置名/错误、review.status=pass|fail|pending与具体reason；汇总拒绝缺例/重复/未评，退出码0全部通过、1失败、2缺环境或待评。

- [ ] **RED：评估门禁及真实恢复脚本隔离。** `test_incomplete_or_unreviewed_eval_cannot_pass`、`test_duplicate_case_ids_and_wrong_model_are_rejected`、`test_recovery_worker_refuses_demo_db_or_data_directory`；恢复worker只允许3307/retailq_test及其临时Lite子目录，持有应用owner锁；通过文件IPC在 after_mysql_pending / after_milvus_upsert 两个边界发ready，父进程确实终止worker，确认已退出再起新进程，不依靠抛异常冒充跨进程中断。
- [ ] **运行红。** `& $ProjectPython -m pytest tests/test_knowledge_evaluation.py -v`，记录未实现脚本/门禁的失败。
- [ ] **GREEN：实现评估与断点证据。** retrieval 至少12条（核心邮费、未见同义改写、拒答、表格、关键条款/不同适用条件），按真正gold来源/段落核对recall@3与逐例正确性；mining用任务6/7标注集实际调用LLM，保存跨批去重决策与来源核验；不要用raw JSON可解析代替语义判断。恢复脚本用真实MySQL+Lite+BGE，对中断两边界分别记录重启前后id集合/chunk数/vector_id/向量数量/pending数，确认不重复新增并可检索；显式reconcile再测done向量丢失。孤立测试确定性向量证据与真实嵌入证据分目录保存。
- [ ] **跑真实模型并逐例判定。** 凭据只放本地.env，先prepare-tokenizer和真实BGE单批smoke，断言返回1024维及语义排序。用retrieval开发样例校准min_score/去重候选阈值，冻结到本地配置及报告，再跑未参与调参的验收样例；不从测试集反向加入“邮费”问法。不保证单一总分覆盖一切：核心邮费须召回并据实回答，拒答/条件/抽取保留样例逐例通过，且两项中断恢复都通过才交付。失败修Prompt按新run输出重评，失败原报告保留。
- [ ] **最终回归与说明。** 完整pytest、pip check、干净锁环境安装/导入/全量测试、git diff --check；DDL副本SHA256一致。若真实BGE key、Docker或依赖不可用，明确未通过的验收，不把mock替代。README给出下方演示命令、单进程/停机维护、旧版知识追加、真实数据未提供、未完成会话不抽取、模型文本会发往配置云端及备份整个关闭后Lite目录等实际边界。
- [ ] **独立 code review。** 按 requesting-code-review 技能对已实现后端的固定base/head做一次新上下文独立审查（执行方式若选SDD，其每任务审查之外仍做整体审查）；输出Critical/Important/Minor和具体证据，保存review文档。当场追记结论。Important/Critical先红→绿修复并重跑受影响真实验收，不能带未解决阻断问题声称完成；是否需复审按技能执行，不虚称第二次独立复核。
- [ ] **finish与本地交付。** 使用 verification-before-completion / finishing-a-development-branch，按仓库main约定提交已验证限定文件，不推送/建PR/worktree。dev-notes即时补finish：最终真实命令/结果、spec/plan/review路径、用户纠偏、所有失败与返工、未验证边界；交用户可执行演示命令、测试结果和dev-notes路径。提交 `feat: verify and document semantic knowledge retrieval`。

## 交付时必须可执行的演示命令

以下目前是待实现CLI的验收契约，不是现在可运行或已经通过的命令。实施者交付时用实际目录/版本复核，并填入真实结果。

```powershell
$ProjectPython = '.\.venv\Scripts\python.exe'
& $ProjectPython -m scripts.knowledge init-db --scope demo
& $ProjectPython -m scripts.knowledge prepare-tokenizer
& $ProjectPython -m scripts.knowledge import --manifest examples/knowledge/manifest.json
& $ProjectPython -m scripts.knowledge import-faq
& $ProjectPython -m scripts.knowledge vectorize
& $ProjectPython -m scripts.knowledge query --text '邮费是多少'
& $ProjectPython -m scripts.knowledge mine
& $ProjectPython -m scripts.knowledge status
```

维护命令结束后启动单worker服务；使用本地.env显式启用KNOWLEDGE_ENABLED并配置独立EMBEDDING_API_KEY。首个示例应回答满99元免运费、未满8元以及结算页例外，且明确演示规则，具体以实际返回为准。

```powershell
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 1
# 另一个终端运行在线smoke；它只走已运行服务，不打开同一Lite文件
& $ProjectPython -m scripts.smoke_knowledge --base-url http://127.0.0.1:8000 --output artifacts/live/ch03/smoke.json
# 离线评估前停止服务；选择全新输出目录，保留旧失败结果
& $ProjectPython -m scripts.evaluate_knowledge --mode retrieval --cases evals/ch03_retrieval.jsonl --output artifacts/evals/ch03/retrieval-run-01
& $ProjectPython -m scripts.evaluate_knowledge --mode mining --cases evals/ch03_mining.jsonl --output artifacts/evals/ch03/mining-run-01
& $ProjectPython -m scripts.verify_knowledge_recovery --scope test --output artifacts/verification/ch03/recovery-run-01
& $ProjectPython -m pytest -q
& $ProjectPython -m pip check
```

mine面对当前没有已结束会话的库可以得到0个候选，须如实报告；正向抽取效果由明确标注的conversations评估集和隔离MySQL历史测试证明，不能把扫描0行称为已验证挖知识效果。在线完整模型聊天用 scripts.evaluate_tools 的 ch03_cases（为脚本添加/沿用 `--cases` 参数）；该步骤在服务运行时执行，不打开Lite文件。

## 计划自检与执行交接

本计划由作者按 writing-plans 的五项清单自检，不派子代理假称“独立计划评审”：spec覆盖、逐步骤可执行、跨任务签名一致、五类Review Focus有归属测试、篇幅与实现细节比例。自检结果和实际文档检查数字在dev-notes落地。

推荐 Native：接口共享较多，按序由同一实现者持有上下文可减少同步成本，最后仍有独立整体code review。用户也可选Subagent-driven（每任务新实现者/审阅者，成本更高）；等待用户审阅计划并选定方法，再加载对应执行技能，不把本轮“制定计划”当作批准开始实现。
