# RetailQ 第 2 章 Function Calling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在现有 SSE 客服聊天中实现五个业务工具的单轮调用、MySQL 持久化与气泡工具徽章，并通过用户三个指定问题验收。

**Architecture:** 原位升级 `/api/chat/stream`，保留 FastAPI 工厂及唯一 ChatOpenAI 网关；API、会话服务、工具与 repository 分层。每次提问最多一次工具执行：一次带工具的模型选择，执行及 ToolMessage 回灌，再一次不绑定工具的真实流式回答；SQLAlchemy 通过短事务访问 Docker MySQL。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、PyMySQL、MySQL 8.4 LTS / Docker Compose、LangChain `langchain_core.tools.tool` 和 `langchain-openai`、Pydantic、AnyIO / asyncio、pytest / HTTPX；聊天页保留原生 HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-10-24-ch02-function-calling-design.md`，2026-10-24 用户确认，原话：“确认继续制定plan”。

**Status:** 2026-10-24 用户选择 Native 执行；任务1至7已实施验收。独立后端审查的3项Important均经作者红→绿修复；最终114项代码测试通过，真实模型run-04的12例逐例核对通过，三项页面验收通过。直接main本地交付，不推送。

## Global Constraints

- 直接在 `main` 开发；不创建工作树、功能分支或 PR，不自动推送。沿用仓库单人工作约定。
- Python `>=3.12,<3.13`，现有 `.venv` 为 3.12.13；不使用 Conda base 安装项目依赖，不改现有模型协议或供应商。
- 四表严格沿用用户 DDL：`faq` / `conversations` / `messages` / `tickets`，包括中文 ENUM、unsigned BIGINT、JSON、外键和时间戳；不加第五张表。DDL 原稿进入仓库，ORM 不作为改写其结构的工具。
- 订单、商品、物流用工具内部随机 mock，返回 `source=demo`；不建对应表、不接真实业务 API。FAQ 使用 SQL LIKE，工单实际落库。
- 每次用户消息最多执行一个业务工具。禁止自动 Agent Loop、并行工具执行、向量检索、RAG；第二阶段模型不提供工具。
- 五工具默认执行等待期限 5 秒；读工具仅对超时和临时连接故障最多重试 1 次。写工单不自动重试、不自动重放 POST；不声称外层取消能杀死数据库线程。
- 保留现有 `message` 8000 字符、history 最多 100 条、严格 Schema、默认 8192/1024/512 预算和 60 秒模型超时。新增工具 schema / 参数 / 结果计入估算，不能丢弃本轮系统、用户及完整工具链来强行过预算。
- `meta` / `token` / `done` / `error` 保留，新增 `tool_status`。最终文本增量来自实际模型 stream；完整回答落库后才发 stop 的 done；截断不视为完整历史。
- `.env`、测试口令、`.venv/`、`artifacts/` 不入库；异常、SSE、评估报告不输出密钥、完整连接串或原始数据库异常。
- 后端可测代码采用 TDD；Prompt、SQL 种子内容以标注样例验证；聊天页直接 Vibe Coding，不套 UI brainstorm / TDD / code review。
- 每阶段完成即时追记 `dev-notes/ch02.md`：用户原话、产物及验证证据、拒绝/纠偏、失败与返工。失败评估保留，不在结束时一次性补写。
- 技术栈走不通时停下向用户说明，不能切成 SQLite、本机 MySQL、字符串路由或其他模型协议来冒充完成。

## Review Focus

1. 浏览器在工单提交后断开：工单、会话状态和结果事实仍可追溯，不能重复创建或让下轮认为尚未执行（任务 2 / 3 / 5）。
2. 首轮 meta 后数据库 ID 恢复、旧 history 导入和其他 user_id：不重复导入、不跨会话读取；同会话并发请求明确冲突（任务 2 / 5，页面实测见任务 6）。
3. 多个/缺 ID/损坏 JSON 工具申请与异常结束标记：不执行非法调用、不静默选第一个、不构造孤立 ToolMessage（任务 4 / 5）。
4. FAQ 中 `%`、`_`、反斜线、中文和“邮费/运费”：字面 LIKE，不误匹配全表；真实模型不得扩展同义词掩盖漏召回（任务 2 / 7）。
5. 超预算及流式失败：计入工具 Schema 和 JSON，按整轮裁剪；首 token 前后失败、EOF、length、DB 最终提交失败均不能错误地报告成功（任务 4 / 5）。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `compose.yaml`, `compose.test.yaml`, `db/init/01-schema.sql`, `db/init/02-seed.sql` | 演示及隔离测试 MySQL、原始 DDL 与四表种子；两个 Compose 项目各自数据卷及端口 |
| `requirements.in`, `requirements.lock`, `requirements-dev.lock`, `.env.example`, `retailq/config.py` | 必要依赖及配置；保留既有锁定版本 |
| `retailq/db.py`, `retailq/models.py`, `retailq/repositories.py` | Engine / Session 工厂、四表映射、短事务与历史/业务数据访问 |
| `retailq/tools/__init__.py`, `schemas.py`, `business.py`, `runtime.py` | 参数类型、五个 @tool、受控上下文、注册/执行/错误/重试 |
| `retailq/gateway.py`, `retailq/context.py` | 一次工具选择、最终禁工具流、完整轮次历史和预算 |
| `retailq/services/__init__.py`, `chat.py`, `retailq/api/__init__.py`, `chat.py` | 会话编排、SSE 事件与依赖；主 app 仅组装资源和路由 |
| `retailq/app.py`, `retailq/schemas.py`, `retailq/errors.py` | 工厂生命周期、HTTP 请求契约与稳定公开错误 |
| `retailq/prompts/customer_service.txt`, `evals/ch02_cases.jsonl` | 本章工具边界与标注用例，不改售后提取 Prompt |
| `retailq/web/app.js`, `index.html`, `styles.css` | Vibe Coding：服务器会话 ID、工具状态与气泡徽章 |
| `tests/support/database.py`, `tests/support/tool_upstream.py`, `tests/conftest.py`, `pyproject.toml` | 测试 MySQL fixture、可控协议上游、异步和 marker 配置 |
| `tests/test_database.py`, `test_repositories.py`, `test_tools.py`, `test_tool_gateway.py`, `test_tool_context.py`, `test_tool_chat.py`, `test_tool_evaluation.py` | 各后端阶段红绿测试；数据库测试标记 mysql |
| `scripts/client.py`, `scripts/evaluate_tools.py`, `scripts/smoke_tools.py`, `examples/ch02-*.json`, `README.md` | 协议客户端、新章评估/演示、UTF-8 示例与运行说明 |
| `docs/superpowers/reviews/2026-10-24-ch02-review.md`, `dev-notes/ch02.md` | 实施后后端独立审查结论及分阶段证据 |

## 执行与测试约定

本计划是一条交付链，不拆成独立子项目；数据、工具、网关及聊天共享协议，必须按依赖顺序集成。前端例外只影响其实现方法，不影响后端集成验收。

PowerShell 命令均在 `D:/AI-Agent/RetailQ` 执行，先定义 `$ProjectPython = '.\.venv\Scripts\python.exe'`。Docker 如未被当前终端 PATH 识别，令 `$Docker = 'C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'`，后文 `docker` 替换成 `& $Docker`；不要为此重装软件或修改系统 PATH。

演示实例使用 `compose.yaml`、项目名 `retailq`、数据库 `retailq`、`127.0.0.1:3306`；独立的 `compose.test.yaml` 使用项目名 `retailq-ch02-test`、数据库 `retailq_test`、`127.0.0.1:3307`。使用不同具名卷，测试 fixture 硬校验库名和端口；严禁清理演示库。Compose 都只含 mysql 服务，Python 应用本地运行，不为不存在的应用容器增加 depends_on。

新增 `MYSQL_HOST/PORT/DATABASE/USER/PASSWORD/ROOT_PASSWORD`，默认 host=127.0.0.1、port=3306、database/user=retailq，口令无默认有效值。使用 `SecretStr` 保存口令，以 SQLAlchemy `URL.create` 组装连接，支持特殊字符；测试实例使用相同本地配置的账户凭据、显式库名/端口覆盖。测试不使用真实公司数据库或账号。

unit 命令为 `& $ProjectPython -m pytest -m 'not mysql' -q`；MySQL 集成命令为 `& $ProjectPython -m pytest -m mysql -q`；最终全量为 `& $ProjectPython -m pytest -q`。缺测试库时 mysql 测试明确失败并给出启动命令，不默认 skip 后称全过。fixture 只允许在确认 `retailq_test:3307` 后按外键逆序清空测试数据、恢复种子，Session 必须关闭；seed 含确定 ID，清理不使用 DELETE 以外的生产迁移捷径。

代码红阶段先写测试并运行确认行为缺失；依赖缺失、Docker 权限或拉取失败不是有效业务红。绿阶段记录实际命令和结果，必要重构后复验；每任务保存限定文件的本地提交，不 stage 全仓库或 push。不得把本计划中的断言或预期输出写成已执行证据。

### Task 1：Docker MySQL、四表与 SQLAlchemy 基建

**Files:** 新建 `compose.yaml`, `compose.test.yaml`, `db/init/01-schema.sql`, `db/init/02-seed.sql`, `retailq/db.py`, `retailq/models.py`, `tests/support/database.py`, `tests/test_database.py`；修改 `config.py`, `requirements.in`, 两份 lock, `.env.example`, `tests/conftest.py`, `pyproject.toml`。

**Interfaces:** `Settings.is_database_configured: bool`, `Settings.database_url: sqlalchemy.URL`（禁止日志完整渲染）；新增 `tool_timeout_seconds=5`, `tool_read_retries=1`（0..1）。`Database(settings: Settings)` 提供 `sessions: sessionmaker[Session]`, `ping() -> None`, `close() -> None`；`models.Base` 为 DeclarativeBase，ORM 类为 `Conversation`, `Message`, `FAQ`, `Ticket`。mysql 测试 fixture 为 `test_database: Database`、`repository: ChatRepository`（任务 2 添加），每例独立重置并恢复 seed。

- [x] **准备配置和依赖。** 先核对 SQLAlchemy 2 + PyMySQL 的当前兼容版本，只在项目 `.venv` 添加 `SQLAlchemy`、`PyMySQL[rsa]` 及必需传递依赖；rsa extra 支持 MySQL 默认 caching_sha2_password 认证，不改服务端认证插件来规避依赖。保留已有运行版本，更新 runtime/dev 锁文件，用临时干净环境验证两份 lock 可安装。口令只写本地 `.env`，不覆盖模型配置；用 `URL.create` 支持 `@:/#` 等字符，不手拼连接串。配置/Compose 文件不以机械单测替代启动验证。
- [x] **建立真实测试库前置。** 原样复制用户 DDL，并比对 SHA256；seed 包含至少 3 条 FAQ、独立示例会话、配对 tool 申请/结果和示例工单，显式 `SET NAMES utf8mb4`。MySQL 固定 `mysql:8.4`、utf8mb4、严格模式、具名卷、只读 init 挂载、健康检查；root 密码不进 CLI 参数或日志。`docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql` 成功后以真实 SELECT 和四表结构证明初始化成功，不能只看 mysqladmin ping。
- [x] **RED：数据库映射/配置测试。** `test_mysql_mapping_matches_supplied_ddl` 断言 ORM/实际库均恰为四表，BIGINT unsigned、JSON、中文 ENUM、外键/索引及更新时间一致；`test_database_password_special_characters` 断言 `settings.database_url.password == supplied_password`，`supplied_password not in repr(settings)`；`test_seed_four_tables_and_faq_gap` 断言四表非空、存在配对 tool_call_id、没有“邮费”字面项。

```python
def test_mysql_mapping_names():
    from retailq.models import Base
    assert set(Base.metadata.tables) == {"faq", "conversations", "messages", "tickets"}
```
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_database.py -v`，记录缺失映射、配置或字符编码行为的失败；依赖/容器应已就绪。
- [x] **GREEN：实现 Database / ORM。** 用 MySQL dialect 的 unsigned BIGINT、原枚举、TEXT/JSON 与 server_default/server_onupdate 映射；不调用 create_all 改写 DDL。Engine 启用 pool_pre_ping 和有界 connect/read/write 超时，Session 不跨线程。`Database.close` 释放引擎；空配置可启动健康/提取服务，但聊天拒绝 503。
- [x] **验证并留痕。** 同命令全过，运行 `pip check`；`docker compose -p retailq -f compose.yaml up -d --wait mysql` 后读取非敏感表结构与 seed，记录实际结果。再次 up 不重建、不重复 seed，重启容器数据保留。记录失败和修复，提交 `feat: add Docker MySQL and four-table persistence schema`。

### Task 2：会话消息、FAQ 与人工工单的数据访问

**Files:** 新建 `retailq/repositories.py`, `tests/test_repositories.py`；补充 `tests/support/database.py`。

**Interfaces:** `ChatRepository(database: Database)` 的方法均为同步、每次自行短事务：`start_turn(user_id: str, conversation_id: int|None, history: list[HistoryMessage], message: str) -> int`；`load_messages(conversation_id: int, user_id: str) -> list[MessageRecord]`；`append_message(conversation_id: int, user_id: str, role: str, content: str|None, tool_calls: list[dict]|None=None, tool_call_id: str|None=None) -> int`；`search_faq(keyword: str) -> list[dict]`；`create_ticket(conversation_id: int, user_id: str, description: str, ticket_type: str, tool_call_id: str) -> dict`。`MessageRecord` 为不可变 DTO，包含 DDL 各消息字段，脱离 Session 后可读取。

- [x] **RED：历史与归属。** `test_start_and_reload_turn` 断言新会话 user_id/status、按 messages.id 恢复顺序、无 ID 时只导入一次完整旧 history；`test_wrong_owner_and_missing_conversation` 对不存在或不同 user_id 均返回同一脱敏 404，且消息数不增加；`test_import_and_server_id_cannot_mix` 断言现有 ID 配非空 history 在写入前拒绝。
- [x] **RED：FAQ 和事务。** `test_faq_literal_like_and_gap` 断言 `search_faq('退货政策')` 非空、`search_faq('运费')` 非空、`search_faq('邮费') == []`，输入 `%`、`_`、反斜线、引号不扩展成全表匹配；`test_ticket_commit_is_atomic` 断言工单号不超过 32 字符、中文类型/状态正确、会话变已转人工。注入 flush/commit 前故障，断言工单数和会话状态均未改变。

```python
def test_faq_hit_and_expected_miss(repository):
    assert repository.search_faq("退货政策")
    assert repository.search_faq("运费")
    assert repository.search_faq("邮费") == []
```
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_repositories.py -v`，使用任务 1 的真实测试 MySQL。
- [x] **GREEN：实现上述方法。** FAQ 对 question/answer/category 做绑定参数 LIKE，转义通配符，固定最多 3 条、每条 answer 最多 1200 字符；只返回必要字段。工单号使用 `T` + 日期 + 随机标识，单事务写 Ticket、更新 Conversation，并保存匹配 call_id 的成功 tool 回执，避免提交工单后断开丢失执行事实；该回执不由服务重复插入。事务/Session 退出时关闭，原始数据库异常不出数据层公开接口。
- [x] **验证断开后的事实。** `test_committed_ticket_receipt_survives_caller_cancellation` 直接读取新建 repository 的 messages/tickets，断言已提交工单对应的 tool 回执与工单号一致、仅一条结果；无工单则不得出现成功回执。`test_detached_records_and_no_session_during_model_wait` 断言 DTO 可离线读取，repository 返回后连接池 checkedout 回到基线。
- [x] **验证并留痕。** 数据库与 repository 测试全过，记录事务故障证据和 keyword 漏召回事实，提交 `feat: persist conversations FAQ lookups and atomic tickets`。

### Task 3：五个 @tool 与执行基础设施

**Files:** 新建 `retailq/tools/__init__.py`, `schemas.py`, `business.py`, `runtime.py`, `tests/test_tools.py`。

**Interfaces:** `ToolContext(conversation_id: int, user_id: str, tool_call_id: str|None=None)` 只来自服务端。`ToolOutcome(payload: dict, receipt_state: Literal['unrecorded','persisted','pending']='unrecorded')` 区分模型可见 payload 与内部回执责任：普通结果由 service 写、已提交工单复用原子回执、未确定写操作由受控收尾任务最终写一次。`build_registry(repository: ChatRepository, context: ToolContext, settings: Settings) -> ToolRegistry`；`ToolRegistry.tools: list[BaseTool]`, `register(tool: BaseTool, *, retry_safe: bool) -> None`, `async execute(call: dict) -> ToolOutcome`。业务 @tool 参数固定为 `query_order(order_id)`, `query_product(product_id)`, `query_logistics(order_id)`, `query_faq(keyword)`, `create_ticket(description, ticket_type)`，隐藏身份用闭包绑定；执行前用含实际 call_id 的 context 构建本轮注册表。

- [x] **RED：Schema 与注册。** `test_exact_five_langchain_tools` 断言名称集合恰为五项、均是 BaseTool、模型 schema 无 user_id/conversation_id/tool_call_id；订单/商品 ID 严格字符串 1..64、keyword 1..64、description 1..2000，未知字段/错误类型拒绝，ticket_type 只能售后/投诉/咨询。`test_unknown_duplicate_and_invalid_tool` 断言重复注册拒绝，未知工具/非法参数不触发业务函数。
- [x] **RED：业务与运行边界。** `test_mock_ids_and_demo_source` 断言三个 mock 保留 `001001` 且返回 demo 标识，不访问 repository；`test_read_timeout_retries_once` 用可控异步函数断言 attempts==2；`test_schema_error_not_retried` 断言 attempts==0；`test_ticket_timeout_not_retried` 断言写函数调用一次、返回成功或明确 outcome_unknown，绝不伪称失败即未执行。额外断言异常中的测试口令不出现在 payload。

```python
# registry 是本文件 fixture：用受控 repository 替身和服务端 ToolContext 构造。
async def test_invalid_id_never_runs_business_code(registry):
    outcome = await registry.execute({"name": "query_order", "args": {"order_id": 1001}, "id": "call_1"})
    assert outcome.payload["ok"] is False
    assert outcome.payload["error"]["code"] == "invalid_arguments"
```
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_tools.py -v`；纯运行控制用 async fixtures，数据库业务复用真实 mysql fixture 并标记 mysql。
- [x] **GREEN：业务与 runtime。** 用 `@tool(args_schema=...)`、Pydantic strict/extra=forbid 定义五工具；mock 在函数内部随机取值，测试断言结构与范围，不断言某次随机值。工具函数异步包装同步 repository，在线程内新建 Session。按注册的 retry_safe 白名单重试，只重试暂时性故障；统一 `{ok:true,data:...}` / `{ok:false,error:{code,message}}`，错误码至少 unknown_tool、invalid_arguments、tool_timeout、tool_execution_failed、outcome_unknown。
- [x] **处理写操作取消。** tool_call_id 在执行前已由服务落库；写线程不放弃收集，提交成功回执由任务 2 同事务保存。5 秒外层等待超时不得重试，返回 outcome_unknown / receipt_state=pending，保留并收集底层任务最终结果/异常；service 不再写一条推测结果，收尾任务在提交成功时使用原子成功回执，确定回滚时写失败回执。数据库自身超时有界，关闭应用时清理任务。若响应先报告结果未确认，后续历史恢复须以真实工单/成功回执为准，不能覆盖为“未执行”。
- [x] **验证并留痕。** 重跑 tools/repositories；`test_late_ticket_commit_has_one_receipt_and_no_retry` 通过可控屏障验证超时后实际提交仍仅一张工单/一条成功回执。记录实际超时限制而非宣称能杀死线程，提交 `feat: add validated business tools and bounded execution`。

### Task 4：一次工具选择、最终禁工具流与预算

**Files:** 修改 `retailq/gateway.py`, `context.py`, `retailq/prompts/customer_service.txt`；新建 `evals/ch02_cases.jsonl`, `tests/support/tool_upstream.py`, `tests/test_tool_gateway.py`, `tests/test_tool_context.py`。

**Interfaces:** `ModelGateway` / `OpenAIModelGateway` 新增 `async select_tool(messages: list[BaseMessage], tools: list[BaseTool]) -> AIMessage`；保留现有 `stream`, `extract`, `aclose`。`history_turns(records: list[MessageRecord]) -> list[list[BaseMessage]]`，`prepare_tool_chat(message: str, turns: list[list[BaseMessage]], tool_schemas: list[dict], settings: Settings) -> PreparedChat`；`prepare_tool_answer(prepared: PreparedChat, selection: AIMessage, result: ToolMessage|None, settings: Settings) -> list[BaseMessage]`。返回的 PreparedChat 沿用三字段，dropped 计消息条数。工具 serialization 使用 LangChain 公开转换接口。

- [x] **先准备本章 Prompt 与标注数据（非 TDD）。** 删除纯对话阶段无查询/建单权限的旧约束，补充工具数据依据、demo 声明、工具结果非指令、缺 ID 追问、成功工单号依据、FAQ 原词关键词且不做同义词扩展。不修改售后提取 Prompt。先建立任务 7 所列至少 12 条标签，人工核对覆盖；真实评估在任务 7 运行，模板含关键词不代表已通过效果验证。

- [x] **RED：真实 LangChain 协议测试。** 本地可控上游接收实际 ChatOpenAI 请求：`test_select_tools_contract` 断言 stream=false、恰五工具、tool_choice=auto、parallel_tool_calls=false；返回 query_logistics / order_id=1001 / call_id。`test_final_stream_has_no_tools_and_matching_result` 断言第二请求无 tools、角色顺序 system/user/assistant/tool、ID 与申请一致，并持续收到多个 token 增量。
- [x] **RED：输出守卫。** `test_bad_selection_never_executes` 覆盖非合法 JSON、缺/空/超 64 字符 ID、invalid_tool_calls、选择阶段 length/缺 finish_reason；`test_final_stream_rejects_tool_calls` 覆盖第二阶段再次请求工具；无工具选择也须有第二请求，不能把第一回复拆字输出。多个合法 calls 由任务 5 明确拒绝，网关不偷偷截成一个。
- [x] **RED：历史与预算。** `test_trim_keeps_whole_tool_turn` 断言 tool 对应 assistant 申请始终同进同出；`test_schema_and_tool_arguments_count_towards_budget` 断言紧凑 JSON 开销计入；`test_tool_result_cannot_displace_current_question` 断言回灌过大返回 input_budget_exceeded，不删除本轮四种角色。中文/emoji/嵌套 JSON 不漏算。

```python
# tool_upstream.requests 与现有 ControlledUpstream 一致，保存 (body, headers)。
# 该 fixture 的选择响应是单个 query_logistics，final 是两个文本增量。
def assert_two_phase_wire_contract(tool_upstream):
    first, final = [entry[0] for entry in tool_upstream.requests]
    assert first["stream"] is False
    assert first["parallel_tool_calls"] is False
    assert len(first["tools"]) == 5
    assert final["stream"] is True
    assert "tools" not in final
    assert final["messages"][-1]["tool_call_id"] == "call_1"
```
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_tool_gateway.py tests/test_tool_context.py -v`。
- [x] **GREEN：网关和上下文。** 选择阶段只调用一次 ainvoke，显式 tools/auto/禁并行；真实端点不支持时明确错误，不能退化成规则选工具。最终阶段复用现有不绑定工具的 stream 与 EOF/finish 守卫，extract 继续无工具。按下一个 user 消息划分历史轮次；只回放完整轮次或带完整申请/结果的已执行工具事实，排除孤立请求和部分正文；回灌预算不足可移除更旧完整轮次，但不得拆本轮工具链。
- [x] **验证中断历史。** `test_interrupted_ticket_turn_preserves_committed_result` 构造 user/assistant(call)/tool(success)、没有最终正文，断言下一轮仍包含完整配对与工单号，不自动再执行工具；`test_pending_request_without_result_not_replayed` 断言孤立申请排除。工具结果按本轮申请 ID 关联，不能只靠邻接顺序；`test_late_receipt_restores_original_turn` 覆盖最后正文先保存、回执后到。单会话已使用的工具 call_id 不可再次作为新申请执行；不往 messages 添加虚构的正常 assistant 回答。
- [x] **验证并留痕。** 新测试及原 `test_gateway.py` / `test_context.py` / extract 全过；必要时仅更新预期已明确变更的聊天测试，保留原 EOF、脱敏、严格 JSON 回归，提交 `feat: add single tool selection and bounded tool context`。

### Task 5：分层 SSE 聊天编排与持久化入口

**Files:** 新建 `retailq/services/__init__.py`, `chat.py`, `retailq/api/__init__.py`, `chat.py`, `tests/test_tool_chat.py`；修改 `app.py`, `schemas.py`, `errors.py`, `scripts/client.py`, `tests/test_chat_api.py`, `tests/test_contracts.py`, `tests/test_evaluation.py`, `tests/support/database.py`。

**Interfaces:** `ChatRequest` 增加 `conversation_id: int|None=None`（strict >0、<= unsigned BIGINT 上限）, `user_id: str='local-demo'`（strict strip，1..64）。有 ID 与非空 history 同传为 422。`ChatService(repository, gateway, settings)` 提供 `async prepare(body: ChatRequest) -> PreparedTurn`、`stream(turn: PreparedTurn) -> AsyncIterator[ChatEvent]`；`PreparedTurn` 包含 conversation_id/user_id/prepared/context 和当前轮次锁租约；`ChatEvent(event: str, data: dict)`。API `get_chat_service(Request)` 提供可覆盖依赖；GET /health 增加 database_configured 布尔值，只表示配置存在。

- [x] **RED：主链路。** `test_sse_logistics_persisted_roundtrip` 断言事件为 meta → tool_status(running) → tool_status(completed) → 多个 token → done；meta 含数据库 ID，工具 ID 一致。DB 中角色恰为 user/assistant(tool_calls)/tool/assistant(final)，内容与回灌一致；第二轮只传 ID 从 DB 取得历史，不需 history。`test_no_tool_still_streams_and_persists` 断言两次模型请求、零工具执行、两条数据库消息。
- [x] **RED：单工具和失败。** `test_multiple_calls_rejected_before_execution` 断言工具执行数 0、无工单、SSE error 无 done；`test_tool_validation_error_is_fed_back` 断言失败 ToolMessage、failed 徽章状态、最终真实流式解释；覆盖未知工具、查询超时、数据库不可用、工具结果写入失败、最终正文 commit 失败、空输出/EOF/length，敏感信息不泄露。
- [x] **RED：取消及隔离。** `test_stop_after_ticket_commit_preserves_ticket` 用真实 TCP SSE 和上游屏障：收到工具完成帧后断开，再查 DB 及下一轮，断言工单事实保留且没有部分 assistant 正文。`test_same_conversation_concurrent_turn_returns_409` 验证同 ID 第二个在写 user 消息前拒绝，不串轮；不同会话不相互阻塞。测试 meta 后立即断开、运行中断开、错 user_id 和旧 history 导入恢复。

```python
# client fixture 绑定真实测试 MySQL 与受控物流上游，固定使用 test-user。
def test_tools_are_visible_before_final_text(client):
    response = client.post("/api/chat/stream", json={"message": "订单1001物流", "user_id": "test-user"})
    events = parse_sse(response.text)  # 复用 tests.test_chat_api.parse_sse
    names = [name for name, _ in events]
    assert names[:3] == ["meta", "tool_status", "tool_status"]
    assert events[1][1]["status"] == "running"
    assert events[2][1]["status"] == "completed"
    assert names.index("token") > 2
    assert events[-1] == ("done", {"finish_reason": "stop"})
```
- [x] **运行红。** `& $ProjectPython -m pytest tests/test_tool_chat.py tests/test_chat_api.py -v`，数据库相关标记 mysql，修改旧 API fixture 使用测试库；原“纯聊天仅一次模型请求”预期改为两次，保留非相关错误守卫。
- [x] **GREEN：实现服务/API。** Schema/预算/身份/并发检查在 SSE 前完成；消息写入短事务后发 meta。选择返回申请后先存 assistant，再发 running，最多 execute 一次；写结果并发 completed/failed 后回灌；工单成功回执复用任务 2 的原子记录，不再 append 一条。只有模型 stop、正文非空且 DB 写入成功才发正常 done；length 可如实发 length 的 done，但不保存为完整 assistant；无正常 finish 或提交失败发 error。旧 history 仅首轮导入，有 ID 时只信 DB。
- [x] **完成取消/锁释放。** 应用只使用单 Uvicorn worker；服务内部用按会话租约避免同 ID 并发，409 明确说明；不把该进程内机制宣称为跨进程锁。预流失败、生成器未开始、客户端取消和正常结束均须关闭上游、释放租约；如果写操作仍 pending，把租约移交受控收尾任务，等待确定结果再释放，期间新请求返回 409。数据库写 worker 的结果按任务 3 收集，成功回执不因取消丢失。app 关闭时释放网关、任务及 Database。
- [x] **兼容脚本与验证。** `scripts.client.chat` 增加 keyword-only `conversation_id: int|None=None, user_id: str='local-demo'`，旧参数兼容；在旧返回字段外保留 conversation_id/tool_status，接受新帧且仍检查终止完整性。对工具 failed 保留实际状态交给工具评估判断，不误标为工具成功；有效错误解释仍可构成完整流。`check_response` 读取公开 error.code，不能再把所有 503 归为 model_not_configured。更新旧评估 helper 的协议测试而不重写其完整性规则。全量测试、真实 TCP 首块/断开测试通过后追记，提交 `feat: integrate persisted tool calling into SSE chat`。

### Task 6：聊天页 Vibe Coding 适配（用户明确例外）

**Files:** 修改 `retailq/web/app.js`, `index.html`, `styles.css`；浏览器证据放忽略目录 `artifacts/ui/ch02/`。

**Interfaces:** 消费任务 5 的 conversation_id 与 tool_status；沿用原页面入口、sessionStorage 会话、流式正文和停止按钮。

- [x] **直接改页面。** 为浏览器持久保存本地演示 user_id，session 记录 database conversation_id；首次老会话可导入已完成 history，一旦有 ID 就不再带 history。meta 收到 ID 时立即保存，避免停止后丢失。运行中的助手气泡显示工具名徽章和正在查询/已完成/失败状态，同 ID 更新，完成后保留，刷新不丢失。
- [x] **保留操作并更新文案。** 正文继续逐增量显示，保留新对话/切换/复制/停止/移动侧栏；新增本章三个示例问题，移除“不能查询/不能建工单”的旧说明，说明订单/商品/物流是演示数据。失败轮次不要在界面写“未执行工具”；停止后徽章保留已发生的状态，未确认的显示未确认。
- [x] **浏览器验证。** 用真实后端依次观察物流 1001、退货政策、邮费的徽章/正文；实际刷新、新建、切换、复制、停止和后续提问；1280×720 与 390×845 检查内容、输入区和徽章无溢出，console 无新增错误。存实际截图；若真实模型评估尚未通过，截图不得标为最终验收。
- [x] **留痕并保存。** 记录实际操作、截图路径和问题修复，提交 `feat: show tool traces in the existing customer chat`。不补 UI 单测、不做 UI 独立 code review；后端协议测试仍需通过。

### Task 7：Prompt/样例评估、演示、后端审查与交付

**Files:** 修改 `retailq/prompts/customer_service.txt`, `README.md`；新建 `evals/ch02_cases.jsonl`, `scripts/evaluate_tools.py`, `scripts/smoke_tools.py`, `tests/test_tool_evaluation.py`, `examples/ch02-logistics.json`, `examples/ch02-faq.json`, `examples/ch02-faq-miss.json`, `docs/superpowers/reviews/2026-10-24-ch02-review.md`；持续更新 notes。

**Interfaces:** `async evaluate_tool_cases(base_url: str, output_dir: Path) -> dict`；复用现有 `scripts.evaluate.summarize_report` 的 expected_case_ids / run_completed / 人工 review 完整性规则。`async run_tool_smoke(base_url: str) -> dict`；CLI 为 `python -m scripts.evaluate_tools --base-url URL --output DIR`、`--review REPORT` 和 `python -m scripts.smoke_tools --base-url URL --output FILE`。

- [x] **核对任务 4 已建立的标注集和 Prompt。** 依据下列标签检查工具说明、数据与实际页面一致；Prompt 与 SQL 内容不做 TDD，真实效果以本阶段评估。不重复新建另一份 FAQ 或只在测试脚本里做关键词路由。
- [x] **确定至少 12 条标签。** `logistics-1001`→query_logistics(order_id='1001')；`faq-return`→query_faq('退货政策')命中；`faq-shipping-gap`→query_faq('邮费')零结果；`faq-shipping-hit`→query_faq('运费')命中；order-001001/product-2001→各自 mock 且说明 demo；ticket-complaint→create_ticket(投诉)及真实 ticket；ticket-missing-detail→追问；missing-order→不臆造 ID；greeting→无工具；injection→不执行任意指令/退款；context-receipt→沿用上一轮已落库工单事实，不重复创建。工具故障与未知结果同时用任务 3/5 的受控测试，不假称真实模型主动生成了故障。
- [x] **RED：评估脚本逻辑。** `test_tool_report_rejects_missing_duplicate_or_interrupted_cases` 断言缺例/重复/未完成/待人工评审均不能 pass；`test_tool_checks_compare_args_and_persisted_result` 断言正确工具但错误 order_id/FAQ 结果/工单号仍 fail；`test_expected_faq_miss_is_a_pass_only_when_no_hit` 断言“邮费”查无符合预期，任何同义改写命中均不通过。

```python
def test_interrupted_report_is_not_pass():
    from scripts.evaluate import summarize_report
    report = {"expected_case_ids": ["faq-return", "faq-shipping-gap"],
              "cases": [], "run_completed": False}
    summary = summarize_report(report)
    assert summary["execution_complete"] is False
    assert summary["status"] != "pass"
```
- [x] **运行红并实现脚本。** `& $ProjectPython -m pytest tests/test_tool_evaluation.py -v` 先见有效失败，再实现脚本收集实际 SSE、调用参数、数据库返回、最终文本、预算/状态和 rubric；必要的 DB 取证在短 Session 内完成、只读脱敏字段。无配置、连接失败、半途退出逐例保存，不把缺例自动补 pass。绿后再运行真实评估。
- [x] **真实模型验收。** 演示库已启动且应用运行后，执行 `& $ProjectPython -m scripts.smoke_tools --output artifacts/live/ch02/smoke.json`；执行 `& $ProjectPython -m scripts.evaluate_tools --output artifacts/evals/ch02/run-01`。逐例读实际选择/参数/结果/回答，填写 review.status/reason，运行 `--review .../report.json` 汇总；修 Prompt 后新建 run-02，不覆盖失败证据。须分别证实物流工具命中、退货政策命中、邮费预期漏召回；协议成功不代替模型能力。
- [x] **补齐演示命令与文档。** README 改为覆盖第 1/2 章，说明配置、Compose 首次初始化/具名卷、端口、真实数据边界、测试库、脚本和页面。启动命令为 `docker compose -p retailq -f compose.yaml up -d --wait mysql`、`& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000`；UTF-8 文件演示为 `curl.exe -N http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch02-logistics.json'`，其余两个文件同理。不提供假定已经存在的数据库 ID；提醒修改 init 脚本不会自动重跑旧卷，不自动 down -v。
- [x] **后端独立审查。** 按 requesting-code-review 调用独立 reviewer，只读审查本章后端、SQL、测试/评估和协议，明确排除 web 三文件的 UI code review；对照获批 spec 与本计划。Important/Critical 在本次修复并复验；任何延期说明原因和影响。使用 receiving-code-review 核实反馈，再先写失败用例后修复。审查证据写入 review 文档，不虚称自查为独立审查。
- [x] **最终新鲜验证。** `& $ProjectPython -m pytest -q`，`& $ProjectPython -m pip check`，`git diff --check`，真实模型 smoke/评估和最终浏览器操作均通过；若审查修复影响工具行为，重跑对应真实用例。工单数/消息配对用 SELECT 核验；提交前只 stage 本章文件，不含 .env/产物/环境。追记后按 finishing 技能交付实际状态，不擅自推送。

## 计划作者自检（规划证据，不是实施完成证据）

- Spec 覆盖：四表/种子/Compose→1；持久化/FAQ/工单→2；五工具/校验/超时重试→3；模型选择/回灌预算→4；单轮/SSE/数据库历史/断开→5；徽章/实际页面→6；Prompt/预期漏召回/真实评估/审查/演示与交付→7。
- 接口一致：Repository 同步，服务/工具在线程包装后使用；身份和 call_id 只由 ToolContext 注入；ToolOutcome 仅 payload 发给模型；成功工单回执只有一次，避免 service 重复落库。
- 五类 Review Focus 各有具名测试；最终回答必须在数据库 commit 后 done，工具已执行但最终中断的轮次仍保留真实工具事实。
- 单轮策略固定：一次选工具，零/一次执行，一次流式回答；没有循环重入模型工具选择。未知合法工具用失败结果回灌，损坏或多个申请则不执行并显式报错。
- UI 例外独立列项，只用浏览器操作/视觉证据验证；没有为 UI 编写 brainstorm/TDD/review 任务。
- 不将预期命令结果写成已通过；没有在制定计划阶段安装依赖、启动数据库或修改产品代码。执行方式与计划仍待用户确认。

## 本轮文档复核

2026-10-24 通过 Context7 再次核对了 LangChain `parallel_tool_calls=False` / ToolMessage、SQLAlchemy LIKE autoescape 与 Docker Compose 初始化/健康语义；复用设计中的库 ID 和来源。认证额外依赖查询补读 [PyMySQL 官方安装文档](https://pymysql.readthedocs.io/en/latest/user/installation.html)，确认 caching_sha2_password / sha256_password 需要 `PyMySQL[rsa]`。实际安装版本、端点支持和 MySQL 行为仍由实施阶段命令证明。
