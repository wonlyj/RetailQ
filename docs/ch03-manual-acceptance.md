# 第3章手工验收与反馈清单

状态：已新增[知识审核工作台与新版验收指南](ch03-knowledge-console.md)，所有对话挖掘入口改为“先审核，再入库”。本地真实MySQL/Lite、跨进程kill恢复已验证；新版分类与真实BGE/网页业务效果待用户反馈。旧B2的自动发布结果仅保留为2026-10-26历史证据，不代表新版人工审核已通过。代理由用户自行处理，开发侧不再尝试。

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

推荐从本机Windows Terminal/PowerShell进入MySQL，明确设置UTF-8 locale和连接字符集。下面方式已由用户在MySQL 8.4.13实际验证，能输入并显示中文；命令在PowerShell执行，不是在mysql>下执行：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
docker compose -p retailq -f compose.yaml exec `
  -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 `
  mysql mysql --default-character-set=utf8mb4 -u retailq -p retailq
```

出现 `Enter password:` 时输入本地 `.env` 的 `MYSQL_PASSWORD`（不是MYSQL_ROOT_PASSWORD；输入时不显示字符），不要把口令发到聊天里。命令中的用户名和库名为本项目默认retailq；若本地改过配置，替换为实际MYSQL_USER和MYSQL_DATABASE。若docker不在PATH，可把docker替换为前面配置的 `& $Docker`。

登录成功看到 `mysql>` 后先执行 `SELECT '中文测试';`，确认能显示中文，再粘贴下面整段SQL；多行输入时出现 `->` 正常，以最后的分号结束并执行。查询完输入 `exit;` 返回PowerShell。Docker Desktop容器终端的 `sh-5.1#` 是Linux Shell，不能直接执行SELECT；若它不能输入中文，改用上面的PowerShell连接方式，不需要改表或重建数据库。若使用数据库GUI客户端，直接在连接retailq库的SQL编辑器中运行即可。

在 `mysql>` 或数据库客户端SQL编辑器执行以下只读SQL，保存结果：

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

先确认本地 `.env` 包含下面两行，然后停止旧Uvicorn进程并重新启动。CLI会临时启用知识模块，因此A3成功不代表在线模块已经开启；在线服务需要显式配置，且启动后不会热读取.env。

```dotenv
KNOWLEDGE_ENABLED=true
KNOWLEDGE_SCHEDULE_ENABLED=false
```

可在PowerShell先做不联网的配置检查：

```powershell
& $ProjectPython -c "from retailq.config import Settings; s=Settings(); print('knowledge_enabled=',s.knowledge_enabled,'knowledge_configured=',s.is_knowledge_configured,'schedule_enabled=',s.knowledge_schedule_enabled)"
```

预期依次是True、True、False。若不符合，检查当前工作目录、.env以及同名进程环境变量。不要用 `/health` 的model_configured/database_configured推断知识模块已开启，这两个字段不检查知识开关。

CLI全部结束后，在旧服务窗口按Ctrl+C并等待退出，再启动：

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

### B1补充：模型把工具调用标记输出到正文

2026-10-26网页反馈中，“双肩包湿了可以晒太阳吗”完成query_faq并回答；随后“运动鞋应该怎么清洁和存放”出现 `<｜｜DSML｜｜ calls>` 等标记。这是模型原生工具语法泄漏到正文，不是正常工具回执。数据库该轮没有标准tool_calls或role=tool记录；上一答保存后约7秒才收到下一问，不是发送间隔太短。真实上游原始响应未保存，不能仅凭记录继续断定是模型本身还是服务端解析器产生问题。

当前后端修复在工具选择及流式正文两处拒绝这类标记，处理跨SSE片段拆开的标记，返回unsupported_tool_protocol错误而不把异常正文当成功答复保存。对已有历史，仅在构造模型上下文时替换异常assistant正文，保留原数据库记录及已完成的工具回执；不会把DSML文本解析成可执行工具或自动再试写操作。

网页验证前重启Uvicorn（不带reload），建议新建对话，再分别问双肩包/运动鞋问题。若仍收到unsupported_tool_protocol，说明异常已被拦截，但该问题的实际客服回答仍未通过，需要记录模型服务返回格式；不能把拦截成功当成正确回答。正常结果应是query_faq工具状态、基于知识中表格的软刷轻刷/避免暴晒，不应出现DSML文本。此次未替用户请求云端重试，真实网页复测待用户反馈。

## B2. 对话挖掘、整体去重和续跑

先停止Uvicorn。只抽取数据库中status为“已结束”、有完整最终助手答复的会话。页面中的“进行中”或“已转人工”不会自动成为挖掘来源；不要批量修改真实会话状态。按A1的PowerShell连接方式登录后，可直接使用中文SQL。

在mysql>下完整执行SELECT，不要单独执行WHERE，也不要把中文状态替换成拼音。先只读检查：

```sql
SELECT id, user_id, status FROM conversations
WHERE status = '已结束' ORDER BY id;
```

### B2.1 查询为空时，准备一条专用演示来源

`Empty set`表示没有符合条件的数据，不是中文输入仍有问题。下一步是准备专用演示会话，然后运行mine。本例为人工构造的客服QA，只验证挖掘到落库的基本链路，不代表真实历史对话质量评估、跨批去重或全部标注集验收。

以下SQL会向演示库新增专用标记为ch03-b2-manual-ebook的1个“已结束”会话、2条消息，不会更新或删除原有会话，也不会直接写知识表。示例选电子说明书，以免只抽到与现有配送/退货知识相同的内容后被正常去重。仅在演示库执行；同一终端串行重复执行会复用该标记及已有消息，不用于并发灌数。

在mysql>下完整粘贴：

```sql
START TRANSACTION;

INSERT INTO conversations (user_id, status)
SELECT 'ch03-b2-manual-ebook', '已结束'
WHERE NOT EXISTS (
  SELECT 1 FROM conversations WHERE user_id = 'ch03-b2-manual-ebook'
);

SET @ch03_demo_id = (
  SELECT MIN(id) FROM conversations WHERE user_id = 'ch03-b2-manual-ebook'
);

INSERT INTO messages (conversation_id, role, content)
SELECT @ch03_demo_id, 'user', '纸质说明书丢了，在哪里看电子说明书？'
WHERE NOT EXISTS (
  SELECT 1 FROM messages WHERE conversation_id = @ch03_demo_id AND role = 'user'
);

INSERT INTO messages (conversation_id, role, content)
SELECT @ch03_demo_id, 'assistant', '演示规则：可在商品详情页的资料下载入口查看电子说明书，具体以页面提供的资料为准。'
WHERE NOT EXISTS (
  SELECT 1 FROM messages WHERE conversation_id = @ch03_demo_id AND role = 'assistant'
);

COMMIT;
```

如果其中任一句报错，先执行 `ROLLBACK;` 并反馈错误，不要继续COMMIT。成功后只读检查：

```sql
SELECT c.id, c.user_id, c.status, m.id AS message_id, m.role, m.content
FROM conversations AS c JOIN messages AS m ON m.conversation_id = c.id
WHERE c.user_id = 'ch03-b2-manual-ebook'
ORDER BY m.id;
```

预期同一个已结束会话、先user后assistant共2条消息。然后执行 `exit;` 回到PowerShell；确认Uvicorn已经停止。

### B2.2 运行抽取、去重和向量化

如果新开了PowerShell，先恢复变量，证据仍保存在本次验收目录；原窗口变量存在时可跳过：

```powershell
Set-Location 'D:\AI-Agent\RetailQ'
$ProjectPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$Evidence = 'artifacts/verification/ch03/manual-20261026-110137'
New-Item -ItemType Directory -Path $Evidence -Force | Out-Null
```

有合适的演示来源后，在PowerShell执行：

```powershell
& $ProjectPython -m scripts.knowledge mine --output "$Evidence/mine-1.json"
& $ProjectPython -m scripts.knowledge status --output "$Evidence/status-after-mine.json"
& $ProjectPython -m scripts.knowledge mine --output "$Evidence/mine-repeat.json"
```

新版首轮应抽到原用户问法及完整答案，阶段到waiting_review、complete=false、尚未发布；这是正常退出0。重复mine复用待审任务，不自动入库。若之前已有等价知识，会产生待人工确认的归并候选。启动单worker服务，打开 `/knowledge` 核对来源，选择最终category后通过或拒绝；通过且完成向量化后才应显示done。准备阶段的complete=false不能当作故障，也不能当作整条链路通过。网页本身可以直接选择该演示来源，无需先运行CLI。

审核完成后先停Uvicorn，再在PowerShell验证新知识能被检索：

```powershell
& $ProjectPython -m scripts.knowledge query --text '没有纸质说明书还能在哪里查看' --output "$Evidence/query-mined-ebook.json"
```

应返回电子说明书的资料下载入口及页面资料限定。不要把演示规则当作真实商家承诺。

历史证据（2026-10-26，旧自动发布版本）：首轮mine为extracted=1/kept=1/vectorized=1/complete=true，重复mine为extracted=0/vectorized=0/complete=true；总知识9条、pending=0、done=9；暂存行kept，新知识id=9且done，改写查询首条返回id=9及完整答案。它证明当时电子说明书单例链路；不算新版人工分类/审核或全标注集验收。

### B2.3 关于langchain-openai代理提示

若出现 `langchain-openai injected a custom httpx transport ... disables httpx's proxy auto-detection`，这是SDK初始化时的WARNING，不是查询失败。它表示SDK为TCP连接选项构造自定义transport时，可能绕过HTTPX对环境变量/Windows系统代理的自动识别；依赖这种隐式代理的聊天模型调用可能受影响，不能把它理解为“全机代理失效”。

本项目langchain-openai=1.7.2；离线知识runtime在聊天配置齐全时也会构建ChatOpenAI网关，所以纯query可能看到这条提醒，但该query实际只调用独立BGE嵌入客户端、Lite及MySQL，不调用聊天LLM。BGE的EMBEDDING_PROXY与这条ChatOpenAI提示分别配置；挖掘网关本身使用项目提供的同步HTTPX客户端、trust_env=False。当前成功报告不因这条警告失效，也不需要重建知识。

官方提供 `http_socket_options=()` 或进程环境变量 `LANGCHAIN_OPENAI_TCP_KEEPALIVE=0` 关闭这项SDK注入；它们不是本项目自建客户端的代理配置开关，不能保证让trust_env=False的已有客户端自动使用系统代理。当前真实流程已成功，暂不为消除警告修改连接配置。若将来聊天/挖掘确实连接失败，再按该实际路径检查，不把warning本身当成失败。

参考：[LangChain http_socket_options官方接口定义](https://reference.langchain.com/python/langchain-openai/chat_models/base/BaseChatOpenAI/http_socket_options)。

新版记录run_id、phase、extracted/review_pending/kept/discarded/vectorized/complete，审计位于 `artifacts/knowledge/reviews/`；旧 `runs/run-*.json` 只读，缺失新增目标ID时页面显示未知。未完成run可用页面“继续任务”或停机后的 `mine --resume '<run_id>'`；waiting_review仍需人工决定。以下原始行查询只核对SQL，不证明来源去向；精确关联请看工作台或只读采集器。人工category可变化，不再按统一“对话知识”筛选：

```sql
SELECT id, batch_no, source_ref, question, answer, status
FROM qa_extraction_staging ORDER BY id;
SELECT id, category, questions, answer, vectorize_status
FROM knowledge_chunks ORDER BY id;
```

语义检查：问法来自真实用户，答案有完整客服证据；跨批次同义同条件合并，不同地区/门槛条件保留；猜测、注入、寒暄、随机订单个人信息不变成通用知识。标注样例在 `evals/ch03_mining.jsonl`，其导出对话在 `examples/knowledge/conversations.jsonl`；当前CLI不直接导入JSONL会话文件，不能把文件存在当作已经用真实LLM跑完。

如果没有合格来源，记“缺测试数据，未测”，不是通过。2026-10-26曾观察到演示库已结束0条；只是当时快照，当前以页面/SQL为准，空暂存不能单独证明抽取失败。新版分类标注评估与GET采集脚本已提供，准确命令见工作台指南；实际语义由用户运行并逐例填写理由。

若其他终端仍不能输入中文，可将两个查询条件分别替换为 `HEX(status) = 'E5B7B2E7BB93E69D9F'`、`HEX(category) = 'E5AFB9E8AF9DE79FA5E8AF86'`。本机已验证中文连接可正常使用，无需日常改写成HEX。

## B3. 定时任务与目录占用

2026-10-26用户明确“B3就不测试了”：本轮人工验收跳过本节，不记为已通过；以下步骤保留供后续参考。

目录占用：在线服务运行时另开PowerShell执行 `scripts.knowledge query --text '邮费是多少'`，应明确报目录被占用，不能第二进程同时打开Lite。停止在线服务后同命令应恢复正常。

定时任务默认关闭。若以后明确开启，配置 `KNOWLEDGE_SCHEDULE_ENABLED=true` 及上海时区时刻，重启单worker；到点仍只准备到waiting_review、complete=false，再由人审核，不自动发布。当天已有last_attempt会阻止重复执行，不删状态冒充首次；测后恢复03:00并关闭开关。当前用户已跳过这项人工验收，本地scheduler门禁回归不冒充该项真实语义通过。

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
