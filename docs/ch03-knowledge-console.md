# 第3章：知识审核工作台

页面入口为 `http://127.0.0.1:8000/knowledge`，也可从客服页左侧“知识工作台”进入。流程为选择已结束对话 → 抽取/全局去重/分类建议 → 人工整组审核 → MySQL原文 → Milvus向量 → 查看逐来源去向。未审核的新知识不会用于客服检索。仓库规则与答复均为演示数据。

本地使用真实MySQL和Milvus Lite的回归、真实进程kill恢复、浏览器操作已验证；开发侧的外部模型使用确定性替身。新版分类Prompt与真实BGE/客服业务效果仍需按下文执行，不能用替身结果代替语义验收。

## 启动（现有环境，不重装）

先打开Docker Desktop，在PowerShell执行：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $Docker compose -p retailq -f compose.yaml up -d --wait mysql
& $ProjectPython -m pip check
```

保留现有 `.env` 的数据库口令、`OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL`；核对以下配置，不把密钥发到聊天：

```dotenv
KNOWLEDGE_ENABLED=true
KNOWLEDGE_SCHEDULE_ENABLED=false
KNOWLEDGE_DATA_DIR=artifacts/knowledge
KNOWLEDGE_TOKENIZER_PATH=artifacts/knowledge/assets/tokenizer.json
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_API_KEY=本地填写
```

有代理需要时使用自己的 `EMBEDDING_PROXY`；开发侧不再代试代理。已有分词器、知识数据无需重建。若首次建缺失知识表，在服务未启动时执行：

```powershell
& $ProjectPython -m scripts.knowledge init-db --scope demo --output artifacts/verification/ch03/console-init.json
```

正式启动保持一个worker，不加reload：

```powershell
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 `/knowledge`。页面及管理API仅允许本机客户端、合法本机Host；写入还校验同源Origin、JSON与管理头。它是本机工作台，没有公网管理员登录系统。关闭页面不取消已受理的任务；停止服务会等待实际维护线程退出，再关闭Lite。

知识runtime初始化失败时，静态页面仍能打开，管理API返回503和脱敏原因，FAQ也明确不可用；不会切换LIKE。修好分词器/目录占用等原因后重启服务，GET不会擅自重新初始化。

## 页面演示与验收

1. 点击“开始挖掘”，选择1–100个不同会话。只允许已结束、首条用户消息/末条完整助手答复的来源；进行中、已转人工、工具调用未闭合的会话禁选。没有来源时可按[原验收清单B2.1](ch03-manual-acceptance.md#b21-查询为空时准备一条专用演示来源)新增专用演示来源，不批量结束真实会话。
2. 等待真实阶段到“等待人工审核”。核对整组全部问法、完整答案、每个来源的冻结消息。准备阶段允许计算去重向量，但不发布原文或索引。
3. 新增候选可搜索已有category或填写新分类。系统建议仅供参考；例如电子说明书的建议可人工改为“售后手册”。通过前验证完整向量文本预算；不修改问答、不拆组、不改变归并目标。
4. 已有知识归并保持目标原文/分类；拒绝单次订单等不适合通用知识的组。一个组覆盖多个来源时，逐来源分别显示“新增”“同组归并”“归并已有”“人工拒绝”，不能把所有discarded当人工拒绝。
5. 到“知识去向”查看确证chunk ID、最终category和向量状态，点击原文核对。planned ID仅为预定，不显示成已入库链接；状态冲突显示无法确证。ID按字符串传递，管理页支持超过JS安全整数的MySQL BIGINT。
6. 刷新/重开页面后仍能查看任务；批准不可再改成拒绝。向量化失败时原文和批准保留，使用“重试向量化”；中断任务使用“继续任务”。两标签页的旧revision可能返回409，需要刷新核对。
7. “终止并封存”需明确确认，等待活跃维护到安全边界。它保留已提交的原文、向量与审计，不是回滚；封存后不能继续旧命令，可创建新任务，未确证处理完的来源可重选。

操作审计损坏时，只依据有效运行/组决定/别名/封存记录建立关联。归属无法确证的文件显示为独立故障记录，明确封存只确认该故障，不替其他任务做决定；可能还需分别处理另一个未结束任务。坏文件原字节保留，不从损坏JSON猜测批准。已applied的来源水位也会重新核对当前SQL成员及目标指纹，无法确证则允许在封存后重选。

同时只允许一个未结束审核任务和一个实际维护作业。网页复用服务内唯一runtime，浏览器操作无需停机。**外部CLI建库/查询/维护仍须先停Uvicorn**，否则应报告目录占用，不启动第二个Lite客户端。

所有对话挖掘入口都遵守先审后发布。`scripts.knowledge mine`只准备至waiting_review，正常退出0且complete=false；重复执行复用待审任务，不自动批准。scheduler默认关闭；显式开启后同样只准备待审，不自动发布。用户此前跳过人工B3，不记为已通过。

## 用户真实分类评估与反馈

保持本地聊天模型配置后，在新的PowerShell窗口执行（此评估不打开Lite，可与页面同时运行）：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
$Evidence = "artifacts/verification/ch03-review/category-$Run"
& $ProjectPython -m scripts.evaluate_knowledge_categories --cases evals/ch03_console_categories.jsonl --output $Evidence
```

输出report.json默认逐例pending，退出2表示待人工评，不是通过。13例覆盖已有类、新类、null、单次订单拒绝、注入、不同条件、多问法、长目录、超预算及人工覆盖。逐项检查完整问答、实际建议/理由、recommend_reject、目录裁剪和参考标签，在每例review中填写pass/fail及具体reason；不要改原始结果。先复制为reviewed.json，再编辑副本，保留原始报告：

运行开始先保存完整样例清单，每取得一例结果即原子更新report.json；Ctrl+C后保留已完成结果、run_completed=false，返回2。部分报告不能汇总通过，完整重测请用新目录，保留中断证据。

```powershell
Copy-Item -LiteralPath "$Evidence/report.json" -Destination "$Evidence/reviewed.json"
& $ProjectPython -m scripts.evaluate_knowledge_categories --review "$Evidence/reviewed.json"
```

汇总0=完整人工通过，1=失败，2=缺环境/待评/报告不完整。标签字符串相同不会自动通过；缺例、重复、无理由、未完成、样例/Prompt/规则哈希不匹配均不能通过。配置模型名不独立证明远端部署身份。改Prompt后换新目录重测。

页面反馈可只读采集实际任务（复制页面任务ID，替换占位符）：

```powershell
$RunId = '替换为页面32位任务ID'
$Capture = "artifacts/verification/ch03-review/console-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
& $ProjectPython -m scripts.smoke_knowledge_console --base-url http://127.0.0.1:8000 --run-id $RunId --output $Capture
```

采集器只GET准备操作、任务、组、来源和确证目标，不批准、不调用模型、不修改审计。不覆盖旧文件；多次GET期间任务可能变化，并非跨资源事务快照。旧版缺失新增ID的记录在页面保持“未知”，不猜测；采集器面向新版任务。

## 独立恢复验收（仅隔离测试库）

先停止本地浏览器fixture及pytest；不可与它们共用3307隔离库。此命令不连接演示3306库，不清演示卷；为每场景添加专用测试来源，保留基线及失败证据。需要已有隔离测试表，可先运行一次测试：

```powershell
& $Docker compose -p retailq-ch02-test -f compose.test.yaml up -d --wait mysql
& $ProjectPython -m pytest tests/test_review_recovery.py -q
$Recovery = "artifacts/verification/ch03-review/recovery-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& $ProjectPython -m scripts.verify_knowledge_review_recovery --scope test --embedding-mode deterministic --output $Recovery
```

真正终止owned worker并wait，然后另一进程重开同目录，覆盖决定、prepared事务、SQL提交和Milvus写入后的精确断点，另含queued/损坏run封存/活跃线程封存。确定性模式明确为deterministic-test，证明存储与恢复协议。

用户自行验真实BGE时换全新目录及模式（会产生嵌入API调用费用；聊天样例仍是fixture）：

```powershell
$LiveRecovery = "artifacts/verification/ch03-review/recovery-live-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& $ProjectPython -m scripts.verify_knowledge_review_recovery --scope test --embedding-mode live --output $LiveRecovery
```

live不代表分类Prompt已验证；仍需上面的真实分类评估。开发侧没有执行live。每场景report.json/summary.json与worker日志保留，不删除失败目录冒充首次成功。

## 数据位置与维护

MySQL的knowledge_chunks是原文权威源，qa_extraction_staging是暂存。Lite默认在宿主机 `D:\AI-Agent\RetailQ\artifacts\knowledge\knowledge.db`；当前Lite版本把它作为目录，不在Docker MySQL容器里。版本2审计在knowledge目录下reviews/operations与reviews/runs，封存标记独立保存；旧版runs/run-*.json只读。

备份时停止所有持有knowledge目录的进程，使用现有MySQL客户端导出关联数据，并复制**整个关闭后的knowledge目录**，包括身份清单、owner文件、审计和Lite目录，成套保存恢复。只拷贝一个.db文件或只恢复向量不能恢复完整来源与决定。不要删除锁文件、down -v或覆盖用户现有库。

代码：retailq/knowledge/review_*负责领域/准备/应用/查询/作业；retailq/api/knowledge*是本机API；retailq/web/knowledge*是页面。设计/计划见docs/superpowers，阶段过程见[dev-notes/ch03.md](../dev-notes/ch03.md)。

开发回归额外使用现有Node.js执行真实前端模块的响应交错测试；会寻找系统Node或Codex附带Node，无npm依赖，运行应用无需Node。整体审查、修复验证及明确延期项见[审查记录](superpowers/reviews/2026-10-28-ch03-knowledge-console-review.md)。
