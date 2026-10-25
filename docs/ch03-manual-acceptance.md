# 第3章手工验收与反馈清单

状态：实现任务1–9已完成；任务10的真实云端语义评估、精确断点跨进程恢复、独立整体审查及finish尚未完成。本清单用于收集实际结果，不代表全章验收通过。按用户要求，代理及网络配置由用户自行处理，开发侧停止继续尝试。

## 0. 测试约定和准备

先测A1–A5，反馈这五项就能定位主要问题。B组是补充验收，可以之后再做。所有规则均为仓库内的演示规则，不代表实际商家政策。

- 使用现有 `.venv` 和 `.env`，不要重新覆盖密钥或数据库口令。
- 离线CLI与在线服务共用一个Lite目录：执行建库、向量化、查询、mine等CLI前，先停止Uvicorn；执行浏览器测试时再启动。保持单worker，不加reload。
- 本清单的建库操作向演示库追加知识，不清空会话、FAQ、工单或知识。修改文档后按新版本追加，不是覆盖旧版本。
- `.env` 中配置 `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1`、`EMBEDDING_MODEL=BAAI/bge-m3`。需要代理时填自己的 `EMBEDDING_PROXY`；本机先前观察到的Mihomo端口为7897，端口可能变化，以你当前配置为准。聊天和挖掘另需已有的 `OPENAI_*` 配置。
- 开启 `KNOWLEDGE_ENABLED=true`；先设 `KNOWLEDGE_SCHEDULE_ENABLED=false`，避免手工验收时自动挖掘改变数据。保留默认 `KNOWLEDGE_DATA_DIR=artifacts/knowledge` 和对应分词器路径。
- 当前 `KNOWLEDGE_MIN_SCORE=0.0` 是尚未经过真实BGE校准的初值。B1的无关问题可能暴露拒答缺陷，应如实反馈，不能据正例命中就认定检索质量通过。
- 云端向量化会发送知识文本和查询；挖掘还会把参与抽取的会话发给配置的聊天服务。样例均为演示数据。

在PowerShell执行：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
$Evidence = "artifacts/verification/ch03/manual-$Run"
New-Item -ItemType Directory -Path $Evidence -Force | Out-Null
$Docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
& $Docker compose -p retailq -f compose.yaml up -d --wait mysql
& $ProjectPython -m scripts.knowledge init-db --scope demo --output "$Evidence/init-db.json"
```

每条命令后可单独执行 `$LASTEXITCODE`：0表示执行完成，1表示执行/部分向量化失败，2表示配置或使用条件不满足。`init-db`只建缺失知识表并校验结构；原用户DDL已用于演示库迁移，可重复执行。固定BGE分词器已在本机准备好；只有文件缺失时才需执行 `python -m scripts.knowledge prepare-tokenizer`，该命令会访问Hugging Face。

## A1. 原文入库和重复导入（不发起嵌入请求）

```powershell
& $ProjectPython -m scripts.knowledge import --manifest examples/knowledge/manifest.json --output "$Evidence/import-1.json"
& $ProjectPython -m scripts.knowledge import --manifest examples/knowledge/manifest.json --output "$Evidence/import-2.json"
& $ProjectPython -m scripts.knowledge status --output "$Evidence/status-before-vectorize.json"
```

检查：

1. 同一份manifest两次返回的 `imported_ids` 完全一致，第二次不会增加chunk数量。
2. 当前三份Markdown正常产生8个块。若库里已有FAQ或其他知识，总数可能大于8；不要据总数不同就删库。
3. 新块先是pending，`vector_id`为空。若此前已成功向量化，同一批块保持done也是正常结果。

在现有MySQL客户端执行以下只读SQL，保存结果：

```sql
SELECT id, category, questions, answer, section_path, content_type,
       is_key_clause, prev_chunk_id, next_chunk_id, vector_id, vectorize_status
FROM knowledge_chunks ORDER BY id;
```

检查：政策/手册的questions是章节标题，category是上级标题路径；FAQ保留原问题；运费标准和退货条件是关键条款。元数据是否被排除在向量文本之外由本地测试验证，不能仅凭SQL列存在就证明。

## A2. 真实BGE向量化和补齐

```powershell
& $ProjectPython -m scripts.knowledge vectorize --output "$Evidence/vectorize-1.json"
& $ProjectPython -m scripts.knowledge status --output "$Evidence/status-after-vectorize.json"
& $ProjectPython -m scripts.knowledge vectorize --output "$Evidence/vectorize-repeat.json"
```

预期：首轮 `failed=0`、`pending_after=0`；status中pending为0、done等于chunks。重复向量化时 `pending_before=0`、`done=0`，原文ID不变。SQL中done块的vector_id为其主键的字符串。

若API调用失败，应保留pending与错误报告；修好本地连接后重新执行同一vectorize命令，应补齐，而非重新导入制造新知识。反馈失败文件里的错误码即可，不要发密钥、完整.env或带认证信息的代理URL。

注意：status的 `vector_verification=not_checked` 明确表示只统计MySQL，不独立证明每条Milvus向量存在；还必须做A3。

## A3. CLI真实语义检索（核心验收）

```powershell
& $ProjectPython -m scripts.knowledge query --text '邮费是多少' --output "$Evidence/query-postage.json"
& $ProjectPython -m scripts.knowledge query --text '买到多少钱才包邮' --output "$Evidence/query-free-shipping.json"
& $ProjectPython -m scripts.knowledge query --text '这个杯子能扔洗碗机洗吗' --output "$Evidence/query-cup.json"
& $ProjectPython -m scripts.knowledge query --text '收货几天之内还能申请退回' --output "$Evidence/query-return.json"
```

逐项核对Top-3中是否有正确知识及完整条件：

| 问题 | 必须召回的事实 |
| --- | --- |
| 邮费是多少 | 普通配送满99元免运费，不满99元8元；偏远地区及特殊商品以结算页为准 |
| 买到多少钱才包邮 | 普通配送满99元；不能承诺所有地区和商品都包邮 |
| 这个杯子能扔洗碗机洗吗 | 柔软工具手洗，不建议放入洗碗机 |
| 收货几天之内还能申请退回 | 签收后7天、完好且不影响二次销售，具体资格和费用以平台售后规则为准 |

输出仍应是 `found/items`，每个item只有 `id/question/answer/category`。此CLI返回检索结果，尚不是聊天LLM最终回答；保留实际items，不只反馈“找到了”。不要为让测试通过给原文补“邮费”同义词。

## A4. 浏览器整链路与旧工具回归

CLI全部结束后启动：

```powershell
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 <http://127.0.0.1:8000/>，每例新建对话，分别输入A3四个问题，记录：是否调用query_faq、实际工具参数、工具回执、最终答复。邮费问题的keyword应保留原问中的“邮费”等文本，避免先改成“运费”掩盖语义漏召回。最终答复须遵循检索内容，保留适用条件，并说明演示性质。

再输入“订单1001物流到哪了”，应调用query_logistics，按原有演示物流回答；不应被FAQ检索接管。退货知识查询不应自动创建工单或执行退款。

若CLI命中而浏览器失败，请明确区分“没调工具”“工具失败”“工具结果正确但回答错误”；这些属于不同故障。页面和stream的tool_status只展示工具状态，不提供完整参数和结果；可按该对话ID在MySQL的messages表查看assistant.tool_calls及role=tool的content。暂时不便查库时只反馈可见证据，不要猜参数。

## A5. 中断后重跑补齐（先测可靠的基础流程）

没有pending时，重复运行vectorize会立即结束，不能证明恢复。A1刚导入而A2还没运行，是现成的待处理起点：先记录status、再运行vectorize；如果任务仍在运行，可在该CLI窗口按Ctrl+C，待进程确实退出，再记录status并重新运行vectorize，最后做A3查询。

预期：原文ID和条数不增加；剩余pending在重跑后变成done；实际查询成功。若8块太少、中断前已经全部完成，请反馈“未命中中断窗口”，这项不记为恢复通过，不要删除原文或向量凑故障。

本步骤是普通中断/重跑检查。计划要求的“提交MySQL后、写Milvus前”及“写Milvus后、回填MySQL前”两个精确边界跨进程强杀，以及done向量丢失后的reconcile，专用真实验收脚本尚未交付，仍由开发侧补齐。现有本地故障注入测试不冒充该项真实验收。

## B1. 拒答、条件及表格补充样例

可在停机后用 `query --text ... --output ...`，或在浏览器新对话逐项试：

| 问题 | 预期/观察重点 |
| --- | --- |
| 你们会员积分什么时候清零 | 没有这项知识，不编造积分政策；CLI误召回也要记录 |
| 偏远地区满99元肯定免邮吗 | 保留结算页条件，不承诺免邮 |
| 不满99元要另付多少 | 普通配送8元，并保留例外 |
| 双肩包湿了可以晒太阳吗 | 擦干、阴凉通风自然晾干，不暴晒 |
| 运动鞋应该怎么清洁和存放 | 表格中软刷轻刷、避免暴晒 |
| 我现在要把这单直接退款 | 不把知识检索描述成退款执行成功 |

大表格复制表头、长文完整句重叠、围栏伪标题等结构情况目前由本地专项测试覆盖。现有小演示表格不足以单独证明“大表格按行切分”通过。

## B2. 对话挖掘、整体去重和续跑

先停止Uvicorn。只抽取数据库中status为“已结束”、有完整最终助手答复的会话。页面中的“进行中”或“已转人工”不会自动成为挖掘来源；不要批量修改真实会话状态。可先只读检查：

```sql
SELECT id, user_id, status FROM conversations
WHERE status = '已结束' ORDER BY id;
```

有合适的演示来源后执行：

```powershell
& $ProjectPython -m scripts.knowledge mine --output "$Evidence/mine-1.json"
& $ProjectPython -m scripts.knowledge status --output "$Evidence/status-after-mine.json"
& $ProjectPython -m scripts.knowledge mine --output "$Evidence/mine-repeat.json"
```

记录run_id、extracted/kept/discarded/vectorized/complete，及 `artifacts/knowledge/runs/run-<run_id>.json` 对应运行记录（文件包含会话正文，反馈前检查隐私）。未完成run可用 `mine --resume '<run_id>'` 续跑；状态完整后重复扫描不应重复新增相同知识。用SQL核对暂存状态和最终知识：

```sql
SELECT id, batch_no, source_ref, question, answer, status
FROM qa_extraction_staging ORDER BY id;
SELECT id, questions, answer, vectorize_status
FROM knowledge_chunks WHERE category = '对话知识' ORDER BY id;
```

语义检查：问法来自真实用户，答案有完整客服证据；跨批次同义同条件合并，不同地区/门槛条件保留；猜测、注入、寒暄、随机订单个人信息不变成通用知识。标注样例在 `evals/ch03_mining.jsonl`，其导出对话在 `examples/knowledge/conversations.jsonl`；当前CLI不直接导入JSONL会话文件，不能把文件存在当作已经用真实LLM跑完。

如果没有已结束演示来源，记“缺测试数据，未测”，不是通过。真实标注集的专用评估脚本仍待开发侧补齐。

## B3. 定时任务与目录占用

目录占用：在线服务运行时另开PowerShell执行 `scripts.knowledge query --text '邮费是多少'`，应明确报目录被占用，不能第二进程同时打开Lite。停止在线服务后同命令应恢复正常。

定时任务：有B2的来源和聊天配置后，把 `.env` 的 `KNOWLEDGE_SCHEDULE_ENABLED=true`，设置上海时区几分钟后的 `KNOWLEDGE_SCHEDULE_HOUR` 和 `KNOWLEDGE_SCHEDULE_MINUTE`，重启单worker服务。到点核对 `artifacts/knowledge/schedule.json` 和运行报告；complete为true且实际有来源处理才算语义挖掘证据。当天已有last_attempt记录会阻止再次执行；遇到这种情况反馈“当日已运行”，不要删除状态文件冒充首次调度。测后恢复03:00及期望的开关。

## 反馈模板

不需要一次测完。优先反馈A1–A5，失败也保留后续能独立完成的结果。

```text
测试环境：本机 / 当前代码版本（可贴 git rev-parse --short HEAD）
A1 原文/重复导入：通过 / 失败 / 未测；两次ID与数量：
A2 真实向量化：通过 / 失败 / 未测；pending_before/done/failed/pending_after：
A3 邮费语义检索：通过 / 失败 / 未测；实际items：
A3 另外三问：逐条结果或JSON文件：
A4 浏览器：工具名称与参数（可见时）、回执、最终答复：
A5 中断重跑：通过 / 失败 / 未命中窗口 / 未测；前后pending和ID：
B组：已测编号、原问题、实际结果、预期差异：
失败命令及退出码：
证据目录：artifacts/verification/ch03/manual-...
```

只需反馈脱敏结果/相关JSON及可见页面结果。请勿发送密钥、数据库口令、完整.env或含认证的代理地址。
