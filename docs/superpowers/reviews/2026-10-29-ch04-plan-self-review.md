# ch04 实现计划作者自审

日期：2026-10-29。作者：本会话主代理。对象：`docs/superpowers/plans/2026-10-29-ch04-hybrid-rag.md`。依据：已批准的 `docs/superpowers/specs/2026-10-29-ch04-hybrid-rag-design.md` 和当前 writing-plans/SKILL.md。

结论：**作者自审通过，待用户审阅计划及确认执行方式**。没有派独立计划审查代理，不把本结论称为独立review或产品验收；后端实施后的独立审查在任务17。所有任务checkbox仍未勾选。

## 1. Spec覆盖

| 已批准设计要求 | 计划负责项 | 可核对的验收 |
| --- | --- | --- |
| Standalone、同系列SDK、原生BM25/chinese/dense+sparse | 1/4/11 | 服务/SDK3.0.2，真实schema及型号BM25；产品不导入Lite |
| dense/BM25各50、原生hybrid_search/RRF、指定重排10 | 4/5 | 真实服务两路过滤、RRF k=60及重排index映射 |
| 首尾证据与预算 | 7 | 精确相关性rank序列；只裁完整chunk，实际编号与prompt一致 |
| 品类先过滤 | 3/4/5 | 来源按品类分层；安全表达式双路下推，未知分类空，不放宽 |
| 单轮归一、只检索侧同义词 | 5/12/14 | 型号/数字/否定保真回退、无历史、无多副本入库 |
| 引用可定位原文/章节/版本 | 3/7/9/10/15/16 | 准确ToolMessage与sources、归档行范围、同n不同轮、越权404和真实点击 |
| 无召回/低相关性/生成自评不足拒答 | 7/8/9/16 | 两种source、原话和正确cid、原子池/assistant；错误不入池 |
| 禁止业务承诺的负面知识 | 7/12/14/16 | 标注Prompt验证、真实不承诺到账及型号不串用 |
| 用户DDL两表、编造回执与复发 | 1/8/13/16 | 原件hash/精确结构、空证据信封、跨轮计数、commit/checkpoint断点 |
| 100题难度/ground-truth/分桶 | 12/13 | 20/80、5桶、family隔离、逐quote映射，query字段限制 |
| 四策略公平Recall/MRR/Faithfulness | 5/13/14 | 一次理解、同snapshot/prompt、布局前指标、真实320组test结果 |
| 拒答不刷分、故障/裁判不完整保留 | 13/14 | N/A及真实分母、作答/拒答/裁判覆盖、逐阶段checkpoint |
| 建库在线同时持库、多worker遗留清理 | 2/6/9/11/16 | 实际跨进程锁、leader、启动/封存/kill、活跃/历史版本切换 |
| 聊天页引用和本地满意度反馈 | 15 | Vibe Coding真实浏览器、刷新恢复/锁定/无网络；不纳前端代码审查 |
| 全程Superpowers与即时留痕/交付 | 每任务/17/18 | 后端RED→GREEN、数据Prompt样例替代、dev-notes/真实证据/演示命令 |

未发现没有归属任务的已批准需求。不把未要求的后台台账页面、反馈后端、多轮理解或新框架列入计划。

## 2. 步骤粒度

- 后端每项给出新/改文件、消费/产出接口、主测试名称与具体断言、RED命令、实现决定、GREEN/提交。相邻任务不提前接入不存在的模块，新的Standalone适配器到任务11再取代旧在线适配器。
- 任务12纯数据直接标注核对，任务14运行真实Prompt样例；未为纯数据或Prompt伪造TDD。任务15保留前端例外，不写前端单测或派代码审查。
- 演示命令用CLI JSON返回的build_id和新运行目录，移除让操作者手填不明来源ID的占位命令。全部pytest显式使用既有ProjectPython。
- 文件/SQL/Milvus恢复协议是计划中的决策，不提前写完整实现；没有产品类函数体代码。任务18才填写实际测试数字/报告hash。

## 3. 类型与跨任务接口

- RetrievalRequest/QueryUnderstanding/EvidenceChunk/Result、CorpusPlan/Snapshot、CitationSnapshot/Bundle、RAGDecision/ConfidencePolicy和事务入口均在共享合同或归属任务定义；后续任务沿用名称。
- ModelText/ToolMessage既有普通业务流保留，新增RAG结构化网关注入，不以模型自评作为外部忠实度裁判结论。
- 先预算/编号，再持久ToolMessage，再生成/原子finalize，最后sources/token/done，避免存10条实际只送5条。
- 计划编写时发现并修正：calibration完整性不是320组，而是20×4=80；test为80×4=320。D_missing的sufficient标签必须含answerable=False，不能空集合all误判True；合法拒答的裁判N/A不视为缺失。

## 4. Review Focus及自审发现的修正

五项Review Focus均对应测试与任务，没有空白归属。重点修正：

1. 根目录内锁不能阻止换data-dir绕过：加入项目canonical协调根目录、数据库绑定与测试注入seam，禁止CLI自动重绑定。
2. 来源路径只有章节文字无法保证精确跳转：导入时记录heading/块序/原文行范围，读取历史归档，不模糊匹配当前文件。
3. 缺校准文件如果阻止KnowledgeRuntime.open，会导致无法管理/建库完成校准：policy只在FAQ生成入口核验，管理和检索诊断不被锁死。
4. 台账commit后文件checkpoint失败：信封run回执在同一SQL事务更新、逻辑run名额防新轮覆盖旧轮，实际kill验证。
5. corpus生效身份变化的阈值漂移：文档明确需重新校准，不能后台静默用旧阈值；这属于已写明的运行约束，未声称自动数据飞轮已完成。

这些修正细化已批准设计，不更改固定技术、表结构或本章范围。若实现发现固定技术本身矛盾，仍停下来问用户。

## 5. 比例与证据边界

草稿重复了大量全局要求，作者压缩各任务重复段落后保留18项与明确接口/测试；计划无实现函数体，以文件/合同/断点/命令为主。没有为了缩短计划而删掉真实多进程、裁判失效或迁移回执验收。

本轮只验证文档结构、任务编号/checkbox、路径、共享名称、需求映射、SQL原件hash及Git diff；没有安装依赖、启动Docker、执行DDL、发模型请求、跑产品测试或写真实成绩。上述未来测试命令都不是本轮通过结果。

Context7实际查询和官方版本回退来源在计划末尾；FastAPI新增来源接口文档仍是任务10实现前的显式门禁，不能声称本轮已查。
