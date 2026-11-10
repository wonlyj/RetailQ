# RetailQ 第六章人工测试指南

日期：2026-11-10。项目：`D:\AI-Agent\RetailQ`。

本指南使用本机既有3308隔离MySQL、19531隔离Milvus和第四章语料，不修改3306演示库。保留已有`.env`中的模型/数据库/向量密钥，不要覆盖。GUI实际点击由用户执行并反馈；自动后端验证与浏览器验收分别记录。

## 1. 启动服务（PowerShell窗口A）

先启动Docker Desktop，然后复制执行：

```powershell
Set-Location D:\AI-Agent\RetailQ
$ProjectPython = '.\.venv\Scripts\python.exe'
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $ProjectPython --version
& $ProjectPython -m pip check
& $Docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait milvus
& $Docker compose -p retailq-ch04-eval -f compose.test.yaml -f artifacts/ch04-eval-compose.yaml up -d --wait mysql

$env:PYTHONIOENCODING = 'utf-8'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
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
$env:WORKFLOW_POLICY_SINGLE_CONFIDENCE_PATH = 'artifacts/evals/ch06/policies-20261110-e/policy_single.json'
$env:WORKFLOW_POLICY_MULTI_CONFIDENCE_PATH = 'artifacts/evals/ch06/policies-20261110-e/policy_multi.json'
$env:WORKFLOW_CHECKPOINT_PATH = 'artifacts/ch06-acceptance/checkpoints.sqlite'

Test-Path $env:RAG_CONFIDENCE_PATH
Test-Path $env:WORKFLOW_POLICY_SINGLE_CONFIDENCE_PATH
Test-Path $env:WORKFLOW_POLICY_MULTI_CONFIDENCE_PATH
& $ProjectPython -m scripts.migrate_workflow --scope acceptance
& $ProjectPython -m scripts.migrate_router --scope acceptance
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 18806 --workers 2 --log-config examples/ch05-logging.json
```

三个Test-Path都应True；第六章迁移显示`Chapter six ticket enum migration verified.`。迁移可重复核查，不清空旧工单/对话。上面的artifacts路径是本机已准备的验收材料；不能通过创建同名空文件来替代策略。

若18806已被本次服务占用，先复用该服务；需要重启时在原窗口Ctrl+C，再运行最后一条命令。不终止未知进程，不使用`down -v`。

## 2. 打开前端（PowerShell窗口B）

```powershell
Start-Process 'http://127.0.0.1:18806/'
```

建议新建一个对话。订单1001、1002、001001为固定演示目录，001001与1001不同；物流仍是随机演示数据，不比较两次物流位置是否相同。浏览器使用本地演示身份，不需要登录或填写手机号。

## 3. 必测项目

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| A | 新对话输入`这个能退吗` | 回复位置出现3张可点订单卡片，显示商品/订单号/金额/状态和演示标识；不追问退款原因，不猜订单号。窗口A停在select_order。 |
| B | 点击订单1001 | 卡片选中后自动继续，无需再发消息；日志同turn_id依次load_order、plan_query、expand_query、retrieve_policy、gate、Agent；回答带可点击政策来源，只有基于本单条件的判断，不声称已退款。 |
| C | 点回复下的`申请退款`，先取消表单 | 单号只读；原因是固定下拉，关闭/取消不提交申请。无需自由填写退款原因。 |
| D | 再开退款表单，选择`不想要了`并确认，尝试快速双击 | 显示同一真实工单号及待处理状态，只创建一张退款工单。刷新后核查仍是该单，不显示已到账。 |
| E | 新对话依次输入`订单1001的物流到哪了`→`它可以退吗`→`还是先查刚才那单的物流吧` | 每轮当前诉求正确；第二轮仍指1001且先订单后政策，第三轮回到物流，不继续退款。 |
| F | 新对话输入`我有订单1001和1002，想知道它们的状态`，再输入`这个能退吗` | 有多个可能对象时仍出现选择器，不自行选最近一单；选1002后按1002继续。 |
| G | 新对话输入`退货政策是什么`，选1001 | 先拿订单再查政策；日志有plan_query和retrieve_policy，但没有expand_query（简单FAQ不扩）。 |
| H | 新对话输入`订单1002尺码不合适想换货` | 识别售后，订单→扩写→政策；未知收货/商品状态时仅必要澄清，不强行给换货资格结论。 |
| I | 新对话输入`港口堆存超期费用的减免申请怎么办`或`把蓝色星期九寄往昨天的负一层` | 归其他，做范围说明或澄清，不查询电商订单/政策，也不硬塞退款流程。 |
| J | 无单号退款出现选择器后刷新页面 | 卡片从后端恢复；点选仍继续原轮，不能重复写用户问题。 |
| K | 出现选择器后，窗口A Ctrl+C，重新执行uvicorn命令，刷新前端后点选 | 继续原暂停点；不丢选择器，不重新消解/分类，不创建新轮冒充恢复。 |
| L | 出现选择器后点`取消本次选择`，再问`订单1002的物流在哪里` | 显示已取消，再次正常查询物流；暂停不能被新文字静默覆盖。 |
| M | 点击政策来源，查看原文；把窗口缩窄或用手机尺寸 | 原文可打开，引用对应条款；卡片、原因下拉、提交/取消均能点，不横向溢出。 |
| N | 新对话输入`我要投诉客服的服务态度` | 仍有两个独立按钮“转人工”“建工单”；不点不执行，普通工单仍有原三类，模拟人工不创建退款。 |
| O | 新对话先查`订单1001的物流在哪里`，再说`不是订单1001，我要退另一单`或`我要退新买的运动鞋，之前那单不要退` | 必须出现选择器；选择1002后按1002处理，不默认沿用被否定的1001。 |

如果操作后提示“结果待核查”，先点核查，不更换订单、原因或重复发送新请求。遇到明确失败可新建对话复现并反馈；停止/刷新不撤销已经确认提交的工单。

## 4. 可选命令行演示

以下4条只观察协议；订单卡片体验请从网页发起，不会把curl发起的会话自动导入浏览器：

```powershell
Set-Location D:\AI-Agent\RetailQ
curl.exe -N http://127.0.0.1:18806/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch06-logistics.json'
curl.exe -N http://127.0.0.1:18806/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch06-no-order-refund.json'
curl.exe -N http://127.0.0.1:18806/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch06-faq.json'
curl.exe -N http://127.0.0.1:18806/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/ch06-other.json'
```

无单号退款/通用退款FAQ应返回order_selector和paused，结束等待，不伪造done。物流与其他路径应正常done。

完整自动演示会创建新的隔离测试会话及一张确认的演示退款单。在与窗口A相同环境变量下另开PowerShell运行（需复制窗口A的环境变量，不能仅复制uvicorn行）：

```powershell
$ProjectPython = '.\.venv\Scripts\python.exe'
$RunDir = 'artifacts/evals/ch06/manual-' + (Get-Date -Format yyyyMMdd-HHmmss)
& $ProjectPython -m scripts.evaluate_router run --base-url http://127.0.0.1:18806 --output $RunDir
& $ProjectPython -m scripts.evaluate_router review --report "$RunDir/report.json"
```

作者未逐条审语义的新报告会退出2待审；真实失败退出1，只有完整证据和逐条语义审阅均通过才退出0，不把待审当报错或通过。

## 5. 反馈模板

```text
第六章人工验收：
A/B 选单及继续：通过/失败（实际现象）
C/D 退款原因及唯一工单：通过/失败（工单号）
E/F 多轮切换及多单歧义：通过/失败
G/H FAQ不扩/售后核心流程：通过/失败
I 其他兜底：通过/失败
J/K 刷新及重启恢复：通过/失败
L 取消后继续物流：通过/失败
M/N 来源、窄屏、旧按钮：通过/失败
O 否定旧单及换对象：通过/失败
若失败：原问题、选择订单、点击顺序、窗口A对应turn_id/错误行；可附截图。
```
