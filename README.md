# RetailQ · 客服聊天工具链

当前提供客服聊天、Milvus Standalone 混合知识检索和[知识审核工作台](docs/ch03-knowledge-console.md)：使用 BGE-M3 向量与 Milvus 原生中文 BM25 召回、RRF 融合及 bge-reranker-v2-m3 重排，回答可查看引用来源。页面入口 `/knowledge`。第四章真实四策略评估已完成，结果和已发现的口语问法误拒问题见[评估说明](docs/ch04-evaluation.md)；人工体验反馈仍待收集。

Python 3.12 + FastAPI + SQLAlchemy + Docker MySQL + LangChain + LangGraph。第六章先做指代消解/口语归一，再用默认大模型识别八类意图；退款退货与售后强制先拿订单，再检索政策，最后交主力Agent判断。缺订单号在原轮中断，聊天卡片点选后恢复。保留真正流式答复及AsyncSqliteSaver持久化。[第六章启动与浏览器验收](docs/ch06-manual-acceptance.md)、[架构](docs/ch06-router.md)、[评估](docs/ch06-evaluation.md)。

## 已实现的工具与边界

| 工具 | 数据来源 |
| --- | --- |
| query_order | 固定演示目录1001、1002、001001，与聊天订单卡片共用；按本地演示身份分视图 |
| query_product / query_logistics | 工具内部随机生成的演示数据，不接真实电商或物流接口，不建对应表 |
| query_faq | 知识就绪时：混合召回、重排并用 MySQL 确认知识内容；证据不足时拒答。关闭或未就绪时明确不可用，第2章 LIKE 仅为历史基线 |
| create_ticket | 用户独立确认“建工单”后写入tickets并关联当前会话；不表示转人工或完成退款，不向主力Agent暴露 |

物流/订单进有界ReAct；商品咨询单查询知识。退款退货/售后按订单→规划→可选扩写→同快照政策检索→生成前闸→Agent执行。仅具体核心售后问题扩写，简单FAQ不扩；库内知识不复制。“其他”由无业务工具Agent澄清，投诉保留两个独立确认按钮。闲聊经过消解与分类后返回固定话术。整轮沿用工具步数、模型请求、token和活动时间预算，暂停恢复不重置已耗额度。退款原因只在用户提交绑定订单的退款单时从固定类目选择；确认创建待处理工单，不执行资金退款。

本项目面向本机演示，监听 127.0.0.1，可使用多个 Uvicorn worker。浏览器 user_id 是本地演示身份，不是登录鉴权；不能作为公网生产系统直接部署。

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
docker compose -p retailq -f compose.yaml up -d --wait mysql milvus
& $ProjectPython -m scripts.migrate_workflow --scope demo
& $ProjectPython -m scripts.migrate_router --scope demo
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 2 --log-config examples/ch05-logging.json
```

打开 [本机聊天页](http://127.0.0.1:8000/)，依次输入：

1. `订单 1001 的物流到哪了`：气泡显示 query_logistics 徽章，按演示物流结果回答。
2. `这个能退吗`：没有已确认订单时弹卡，选1001后继续原轮，依次拿订单、扩写、检索政策并判断。需第六章两份独立校准策略，按下方隔离指南配置。
3. `邮费是多少`：开启知识功能、完成运费知识向量化后，应语义召回运费演示规则并保留例外条件。第2章LIKE基线曾漏召回“邮费”，历史报告仍保留该时期结果。

第四章知识建库及校准配置见[混合检索指南](docs/ch04-hybrid-rag.md)。第3章[工作台指南](docs/ch03-knowledge-console.md)和[验收清单](docs/ch03-manual-acceptance.md)保留历史流程，其中 Milvus Lite 配置不适用于第四章。默认关闭定时挖掘；网页、CLI和显式启用的scheduler都只准备待审核知识，通过后才发布。

还可查询商品编号2001、订单001001，或输入“我要投诉”查看两个独立按钮；仅文字表达不自动写工单。气泡显示工具执行中/完成/失败；停止或刷新不会撤销已明确确认且已提交的工单。支持新建/切换对话、复制、停止和移动端侧栏。首次体验第六章优先按隔离环境[人工指南](docs/ch06-manual-acceptance.md)，不必迁移现有演示库。

首次在空具名卷启动时，MySQL执行db/init目录下01–06 SQL；01/02建立业务表与种子数据，其余为知识/RAG/工作流表。再次启动保留数据，不重复初始化。旧卷需显式运行对应章节迁移，第五章为migrate_workflow，第六章为migrate_router（扩展退款ENUM）；不要用down -v清理用户会话。

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

SSE data均为JSON：meta→workflow节点事件/可选tool_status→token→可选sources/actions→done。缺订单时为order_selector→paused，点选走同turn的resume端点；取消返回cancelled。paused不是done，新文字不能覆盖等待。GET pending恢复卡片，GET turn核查未知结果；接口见[架构](docs/ch06-router.md)。actions绑定原助手消息，完整提交后才可操作。无需工具时没有tool_status；流内故障返回error并结束，提交失败不伪报done；length/中断不保存完整助手回答，已执行事实继续保留。token帧不保证恰好等于一个tokenizer token。

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

第六章按[指南](docs/ch06-manual-acceptance.md)配置3308/19531隔离环境与两份校准策略后执行：

```powershell
$RunDir = 'artifacts/evals/ch06/run-' + (Get-Date -Format yyyyMMdd-HHmmss)
& $ProjectPython -m scripts.evaluate_router run --base-url http://127.0.0.1:18806 --output $RunDir
& $ProjectPython -m scripts.evaluate_router review --report "$RunDir/report.json"
```

真实Prompt评估命令为`python -m scripts.evaluate_router prompts --output <全新artifacts/evals/ch06目录>`，冻结58条样例；正式HTTP全图冻结14场景/21轮，含同轮暂停恢复、物流→退款→物流和唯一退款工单。原始completion、SQL、checkpoint、政策归档和可执行SHA均入证据；作者逐条语义审阅后才可通过。退出0通过、1失败、2待审。修改代码/Prompt使用新目录，保留失败报告，不能用旧manifest冒充当前版本。默认模型配置名不等于已验证供应商真实部署身份。

第五章`evaluate_workflow`/`smoke_workflow`及第2章工具评估保留历史用途，冻结标签不改为第六章标签，也不作为新版验收入口。底层政策分数闸独立测试中的误放与正式分流边界分别报告，见[第六章评估](docs/ch06-evaluation.md)。

售后提取继续使用 `/api/after-sales/extract`，字段为 order_id/request_type/expected_solution；使用 LangChain with_structured_output 的 json_mode 或 json_schema，不绑定业务工具。`examples/after-sales.json` 和 `scripts.smoke` 保留兼容；第 1 章历史评估是当时纯对话边界的证据，不直接代表第 2 章策略。

## 代码与过程记录

API→WorkflowChatService→LangGraph/AgentSteps→原ToolRegistry/ModelGateway→Repository/MySQL；独立SQLite checkpointer。第五章显式新增workflow_turns/actions两张元数据表；工单写入与成功工具回执同事务，不再绑定转人工。模型等待不占数据库连接。

- 第6章：[设计](docs/superpowers/specs/2026-11-09-ch06-router-design.md)、[计划](docs/superpowers/plans/2026-11-09-ch06-router.md)、[开发记录](dev-notes/ch06.md)；浏览器点击待用户反馈。
- 第5章设计/计划：docs/superpowers/specs/2026-11-05-ch05-workflow-agent-design.md、docs/superpowers/plans/2026-11-05-ch05-workflow-agent.md
- 第5章开发记录：[dev-notes/ch05.md](dev-notes/ch05.md)；2026-11-06用户已按人工指南执行完成，反馈无报错。

- 第3章设计/计划：docs/superpowers/specs/2026-10-27-ch03-knowledge-console-design.md、docs/superpowers/plans/2026-10-27-ch03-knowledge-console.md
- 第3章开发记录：dev-notes/ch03.md；新版真实分类/审核链路仍需用户按指南反馈。
- 以下第2章设计、评估、审查为历史证据，不代表新版知识链路真实验收：

- 设计：docs/superpowers/specs/2026-10-24-ch02-function-calling-design.md
- 计划：docs/superpowers/plans/2026-10-24-ch02-function-calling.md
- 开发过程与实际测试结果：dev-notes/ch02.md
- 后端审查：docs/superpowers/reviews/2026-10-24-ch02-review.md
- 真实评估与页面截图：artifacts/evals/ch02/、artifacts/ui/ch02/（本地产物，不入库）

## Git 工作方式

单人开发直接在 main 修改、验证和本地提交；按用户指示再推送 origin/main。默认不创建功能分支或 PR。提交前检查限定文件，保持 .env、.venv、artifacts、.superpowers 不入库；不强制推送、不删除数据卷。
