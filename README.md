# RetailQ · 客服聊天工具链

Python 3.12 + FastAPI + SQLAlchemy + Docker MySQL + LangChain。第 2 章在现有 SSE 聊天页接入 Function Calling：模型选择一个工具，执行、回灌结果，再生成真正的流式答复。保留第 1 章售后结构化提取。

## 已实现的工具与边界

| 工具 | 数据来源 |
| --- | --- |
| query_order / query_product / query_logistics | 工具内部随机生成的演示数据，不接真实电商或物流接口，不建对应表 |
| query_faq | MySQL faq 表的字面关键词 LIKE 查询 |
| create_ticket | 写入 tickets，关联当前会话并更新为已转人工；不表示即时接通人工或完成退款 |

每次用户消息最多执行一个业务工具；没有 Agent Loop、向量检索或 RAG。第一次模型请求绑定工具；第二次请求不提供工具，逐上游文本增量转发。工具参数由 Pydantic 严格校验；读工具超时/临时连接错误最多重试一次，工单写入不自动重试。写入超时只能说明结果未确认，成功工单及回执会保留。

本项目面向本机演示，监听 127.0.0.1、单 Uvicorn worker。浏览器 user_id 是本地演示身份，不是登录鉴权；不能作为公网生产系统直接部署。

## 安装与配置（Windows PowerShell）

先运行 Docker Desktop，使用 WSL 2 / Linux containers。Docker Engine 与 Compose 应可用：

```powershell
docker version
docker compose version
```

Python 固定 3.12；不要使用 Conda base 的 Python 安装项目依赖：

```powershell
cd D:\AI-Agent\RetailQ
py -3.12 -m venv .venv                 # 已有 .venv 时跳过
$ProjectPython = '.\.venv\Scripts\python.exe'
& $ProjectPython --version
& $ProjectPython -m pip install -r requirements-dev.lock
& $ProjectPython -m pip check
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

仅运行服务可安装 requirements.lock。SQLAlchemy 和 PyMySQL[rsa] 已锁定；rsa 认证依赖适配 MySQL 默认认证，不需要改成旧认证插件。若 py 未注册 Python 3.12，请使用实际 Python 3.12 完整路径建立环境。

在本地 `.env` 配置模型和 MySQL（已有模型配置请保留）：

```dotenv
OPENAI_BASE_URL=https://your-openai-compatible-service.example/v1
OPENAI_API_KEY=your-local-key
OPENAI_MODEL=your-model-name
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=retailq
MYSQL_USER=retailq
MYSQL_PASSWORD=your-local-database-password
MYSQL_ROOT_PASSWORD=your-local-root-password
TOOL_TIMEOUT_SECONDS=5
TOOL_READ_RETRIES=1
```

占位符必须替换。模型需支持 Chat Completions 的工具调用；不支持时明确报错，不切换供应商或退化成关键词路由。口令通过 SQLAlchemy URL.create 组装，支持特殊字符；不记录完整连接串。其余预算与结构化提取配置见 `.env.example`。本机开发过程中已生成数据库口令，现有 `.env` 无需重新覆盖。

## 启动与浏览器演示

```powershell
docker compose -p retailq -f compose.yaml up -d --wait mysql
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [本机聊天页](http://127.0.0.1:8000/)，依次输入：

1. `订单 1001 的物流到哪了`：气泡显示 query_logistics 徽章，按演示物流结果回答。
2. `退货政策是什么`：query_faq 命中演示退货规则。
3. `邮费是多少`：query_faq 保留“邮费”原词，查无结果并说明未查到。这是本章刻意保留的关键词漏召回；种子里“运费”能查到，“邮费”没有同义词扩展。

还可查询商品编号 2001、订单 001001，或说明配送问题后明确要求创建投诉工单。气泡显示工具执行中/完成/失败；停止或刷新不会撤销已提交的工单。支持新建/切换对话、复制、停止和移动端侧栏。

首次在空具名卷启动时，MySQL 按 db/init/01-schema.sql（用户 DDL 原稿）和 02-seed.sql 建四表、灌数据。再次启动保留数据，不重复执行初始化。修改初始化脚本不会自动更新旧卷；不要用 down -v 清理用户会话。

Docker 安装后旧终端若不识别命令，可新开 PowerShell，或使用本机路径：

```powershell
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $Docker compose -p retailq -f compose.yaml up -d --wait mysql
```

## API 与 SSE

```powershell
curl.exe -N http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch02-logistics.json'
curl.exe -N http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch02-faq.json'
curl.exe -N http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch02-faq-miss.json'
```

请求示例：`{"message":"订单1001物流","user_id":"curl-demo"}`。meta 返回数据库 conversation_id，后续带相同 user_id 和该 ID；历史从 messages 加载。旧客户端可以只在无 ID 的新会话导入完整 user/assistant history；有 ID 时不能再传非空 history。接口拒绝未知字段和客户端 system/tool 消息。

SSE data 均为 JSON：meta（会话 ID、近似输入预算、裁剪条数）→ tool_status（tool_call_id、name、running/completed/failed）→ token（真实增量）→ done（实际 finish_reason）。无需工具时没有 tool_status。流内故障返回 error 并结束，最终正文提交失败不会伪报正常 done；length/中断不保存为完整助手回答，已执行的工具事实继续保留。token 帧不保证恰好等于一个 tokenizer token。

GET /health 的 model_configured/database_configured 只表示配置存在，不证明上游或数据库实时可用。输入/预算错误在流前返回 422；会话归属不匹配/不存在为 404；同会话处理上一轮时为 409；未配置模型或数据库为 503。

## 测试与真实模型评估

测试使用独立 MySQL：retailq_test / 127.0.0.1:3307 / 独立具名卷。fixture 只允许清理该测试库，不碰演示库：

```powershell
docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql
& $ProjectPython -m pytest -m 'not mysql' -q   # 不依赖 MySQL 的测试
& $ProjectPython -m pytest -m mysql -q         # 真实 MySQL 集成测试
& $ProjectPython -m pytest -q                 # 完整验收；测试库缺失则明确失败
& $ProjectPython -m pip check
```

服务已运行时执行：

```powershell
& $ProjectPython -m scripts.smoke_tools --output artifacts/live/ch02/smoke.json
& $ProjectPython -m scripts.evaluate_tools --output artifacts/evals/ch02/run-01
& $ProjectPython -m scripts.evaluate_tools --review artifacts/evals/ch02/run-01/report.json
```

smoke 自动核对三项核心工具选择、数据库结果和流完成情况。完整评估含 12 个标注样例，保存实际工具参数/结果、最终文本及工单落库证据；逐例填写 review.status（pass/fail）与具体 reason 后才能汇总通过。退出码 0 为通过、1 为失败、2 为缺环境/待评。缺例、重复、中断或未评不能报全部通过。修 Prompt 后换新 output 目录，保留旧失败证据。配置模型名不代表已独立验证供应商真实部署身份。

售后提取继续使用 `/api/after-sales/extract`，字段为 order_id/request_type/expected_solution；使用 LangChain with_structured_output 的 json_mode 或 json_schema，不绑定业务工具。`examples/after-sales.json` 和 `scripts.smoke` 保留兼容；第 1 章历史评估是当时纯对话边界的证据，不直接代表第 2 章策略。

## 代码与过程记录

API → ChatService → ToolRegistry / ModelGateway → ChatRepository → SQLAlchemy；会话及消息短事务，不在模型等待期间占用数据库连接。四表为 faq、conversations、messages、tickets；工单与会话状态和成功 tool 回执同事务。

- 设计：docs/superpowers/specs/2026-10-24-ch02-function-calling-design.md
- 计划：docs/superpowers/plans/2026-10-24-ch02-function-calling.md
- 开发过程与实际测试结果：dev-notes/ch02.md
- 后端审查：docs/superpowers/reviews/2026-10-24-ch02-review.md
- 真实评估与页面截图：artifacts/evals/ch02/、artifacts/ui/ch02/（本地产物，不入库）

## Git 工作方式

单人开发直接在 main 修改、验证和本地提交；按用户指示再推送 origin/main。默认不创建功能分支或 PR。提交前检查限定文件，保持 .env、.venv、artifacts、.superpowers 不入库；不强制推送、不删除数据卷。
