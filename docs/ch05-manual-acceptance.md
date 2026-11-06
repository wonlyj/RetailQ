# 第五章人工测试指南

日期：2026-11-06。项目：`D:\AI-Agent\RetailQ`。本指南使用已有的3308隔离MySQL、19531隔离Milvus和第四章已校准语料；不修改3306演示库。浏览器实际布局、点击和刷新验收由用户执行并反馈，自动化结果不替代此项。

## 1. 启动（PowerShell窗口A）

先打开Docker Desktop，保留本机已有`.env`里的模型与数据库密钥，不要覆盖。以下路径对应当前机器已准备好的第四章验收环境。

```powershell
Set-Location D:\AI-Agent\RetailQ
$ProjectPython = '.\.venv\Scripts\python.exe'
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $ProjectPython --version
& $ProjectPython -m pip check
# 环境缺包时才执行：& $ProjectPython -m pip install -r requirements-dev.lock
& $Docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait milvus
& $Docker compose -p retailq-ch04-eval -f compose.test.yaml -f artifacts/ch04-eval-compose.yaml up -d --wait mysql

$env:PYTHONIOENCODING = 'utf-8'
$env:MYSQL_PORT = '3308'
$env:MYSQL_DATABASE = 'retailq_test'
$env:MILVUS_URI = 'http://127.0.0.1:19531'
$env:MILVUS_COLLECTION_PREFIX = 'eval_ch04'
$env:KNOWLEDGE_DATA_DIR = 'artifacts/ch04-eval-knowledge'
$env:KNOWLEDGE_TOKENIZER_PATH = 'artifacts/knowledge/assets/tokenizer.json'
$env:KNOWLEDGE_ENABLED = 'true'
$env:KNOWLEDGE_SCHEDULE_ENABLED = 'false'
$env:RERANK_REUSE_EMBEDDING_KEY = 'true'
$env:RAG_CONFIDENCE_PATH = 'artifacts/ch04-eval-knowledge/rag-confidence-relevance-v2.json'
$env:WORKFLOW_CHECKPOINT_PATH = 'artifacts/ch05-acceptance/checkpoints.sqlite'

Test-Path $env:RAG_CONFIDENCE_PATH
Test-Path $env:KNOWLEDGE_TOKENIZER_PATH
& $ProjectPython -m scripts.migrate_workflow --scope acceptance
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 18805 --workers 2 --log-config examples/ch05-logging.json
```

两个`Test-Path`都应为True，迁移应显示`Workflow metadata migration verified.`。这是幂等结构核查，不会清空现有会话。`artifacts/ch04-eval-compose.yaml`和知识目录是本机已有产物，迁移到其他机器时需按第四章指南准备匹配语料和策略；不能仅创建同名空目录。

如果18805已被本次验收服务占用，直接复用该页面；若需重启，在原服务窗口Ctrl+C后重新运行最后一条命令。不要终止不明PID。不要使用`docker compose down -v`。

## 2. 打开网页（PowerShell窗口B）

```powershell
Set-Location D:\AI-Agent\RetailQ
$env:PYTHONIOENCODING = 'utf-8'
Start-Process 'http://127.0.0.1:18805/'
```

第一次建议新建对话。观察窗口A的JSON日志，`node`表示执行节点，`turn_id`串联同一轮。每个新问题都会有一次意图分类；“闲聊不调用主力Agent”不等于零模型调用。

## 3. 必测五项

| 操作 | 预期与反馈重点 |
| --- | --- |
| 输入`退货政策是什么` | 日志依次包含retrieve、gate；通过闸后才出现agent_decide、answer。正文有来源编号，点击可查看对应归档，不应承诺退款日期。 |
| 输入`订单1001的物流到哪了` | 工具徽章query_logistics，按工具回执答复并标“演示”；本轮不出现retrieve/gate。订单/物流是随机演示数据，不与前次随机结果强行比较。 |
| 新对话输入`我要投诉` | 安抚回复下面有两个独立按钮：转人工、建工单。不点任何按钮，继续输入`你好`，仍正常对话，不自动写工单。 |
| 输入`你好` | 固定闲聊话术；日志无agent_decide、tools、retrieve。 |
| 输入`先查订单1001的状态，再查物流最新位置，两个都要` | 先query_order再query_logistics，agent_decide/tools多次交替。缺失事实不编造。 |

## 4. 两个按钮必须分开验收

1. 新对话说`我要投诉`，点击“转人工”，在确认交互中确认。页面应依次显示`已转接人工客服`和`您好,我是客服小猫,请问有什么可以帮您的`。这是前端模拟，不接真人，不请求建票API。
2. 同一回复中的“建工单”仍可单独使用。打开后先取消，取消不写票据；再打开，填写`订单1001物流延迟，请协助核查`，类型选“投诉”，明确确认后才创建。成功后显示工单编号。
3. 另一个新对话先建工单，再独立点击“转人工”，验证顺序互不绑定。只点一个也应正常。
4. 连点提交、刷新后再查看状态，工单应仍为同一编号。输入`刚才工单编号是什么？`，回答应复述该编号。
5. 提交期间切换会话，结果必须留在原会话；结果未知时仅点击“核查状态”，不自动重复提交。可在浏览器开发者工具Network观察POST与GET：取消无POST，确认才POST，未知核查用GET。
6. 输入`商品坏了，自动回复帮不上忙，请让我选择联系人工或建工单`，Agent也应能给两个建议选项，未点击时不产生票据。

可选SQL核对（窗口B，复用窗口A同样的MYSQL_PORT/MYSQL_DATABASE环境，或本代码显式指定）：

```powershell
Set-Location D:\AI-Agent\RetailQ
@'
from sqlalchemy import select
from retailq.config import Settings
from retailq.db import Database
from retailq.models import Ticket
d=Database(Settings(mysql_port=3308,mysql_database='retailq_test'))
try:
    with d.sessions() as s:
        print(s.execute(select(Ticket.ticket_no,Ticket.conversation_id).order_by(Ticket.created_at.desc(),Ticket.ticket_no.desc()).limit(15)).all())
finally:
    d.close()
'@ | & .\.venv\Scripts\python.exe -
```

按会话ID和新编号比对点击前后，勿以全表总数推断某一按钮的效果。已有自动评估也会留下隔离验收票据。

## 5. 补充边界

- 输入`查一下快递`：追问订单号，不凭空调工具。
- 输入`MH-LP100的充电功率是多少瓦`：资料缺少功率时应承认未知，不编造瓦数。
- 输入`量子传送机的退货政策是什么`：查看gate结果。相关性得分可能仍放行泛化的退货规则；不能据此宣称闸能判断所有事实充分性。无该商品专属政策时应明确范围。
- 流式输出时点停止，再继续问：不保留半截完整答复，不重放旧工具；重启窗口A后相同会话仍能继续。SQLite文件不得被删除或跨数据库共用。
- 缩窄窗口测试按钮换行、确认弹窗、来源面板；刷新后按钮来源应仍对应原回复。

## 6. 裸循环与评估命令

先看不经过LangGraph的最裸循环（controlled是标明的模拟模型，工具为已有真实代码）：

```powershell
Set-Location D:\AI-Agent\RetailQ
& .\.venv\Scripts\python.exe -m scripts.demo_agent_loop --mode controlled
# 实际模型，可产生费用：
& .\.venv\Scripts\python.exe -m scripts.demo_agent_loop --mode real --question '先查订单1001状态，再查物流位置，两个都需要。'
```

真实流程评估需在与窗口A相同的环境变量下运行：把第1节所有`$env:`赋值复制到窗口B执行（A窗口的赋值不会自动同步到B），不要重复执行阻塞的uvicorn命令。v2会先核对服务端3308数据库摘要，错误身份时不发送聊天或分类模型请求。每次新目录，不覆盖历史：

```powershell
$RunDir = 'artifacts/evals/ch05/manual-' + (Get-Date -Format yyyyMMdd-HHmmss)
& .\.venv\Scripts\python.exe -m scripts.evaluate_workflow run --base-url http://127.0.0.1:18805 --output $RunDir
& .\.venv\Scripts\python.exe -m scripts.evaluate_workflow review --report "$RunDir/report.json"
```

`smoke_workflow`是同一完整采集器入口，亦会执行21条分类、12条流程与一张隔离工单确认，不是只发健康检查。退出2表示待人工逐例核对，不能改标签补齐通过。

## 7. 反馈模板

逐项填“通过/失败/未测”，附具体输入、实际文本、会话ID、turn_id或发生时间。重点反馈：五项必测、两个按钮各自确认/取消/都不点、工单编号续聊、刷新与切换、窄屏与来源。出现问题可直接贴浏览器Console报错及对应终端日志，勿包含API密钥。
