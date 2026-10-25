# RetailQ 第 3 章：知识库与 dense 语义检索

状态：2026-10-25 用户确认设计并要求“继续制定实现计划”；独立 builder check 已返回 `nothing important left`。本文据此定稿，下一阶段仅制定计划，不把该确认解释为实现计划已经获批。技术方案为硅基流动 BGE-M3 API，以及通过 pymilvus 使用 Milvus Lite 本地文件库。

## 意图、目标与明确不做

把现有客服聊天工具 query_faq 的内部关键词 LIKE 升级为 dense 语义检索，让“邮费是多少”能找到以“运费标准”表述的知识并据实回答。保留其他四个工具、单轮选工具/回灌/流式回答、售后结构化提取和原聊天页。

本章同时交付 Markdown 离线处理、历史客服对话批量抽 QA/暂存/整体去重、MySQL 原文与 Milvus 向量双写恢复、每日定时抽取任务及可复现验收。使用用户提供的两表 DDL，不擅自修改字段或新增持久表。

不做关键词召回、混合检索、稀疏向量、多向量检索、重排、Agent Loop、模型训练或微调、知识编辑后台、增量文档版本替换/删除、分布式调度和多 worker 部署。模型官方推荐的混合/重排不进入本章。

## 用户已经决定的约束

- 嵌入模型固定 BGE-M3，部署方式为硅基流动 API；拟固定模型 ID `BAAI/bge-m3`，不自动切换 Pro 版或其他模型。
- 向量数据库固定 Milvus Lite，本地文件路径交给 `pymilvus.MilvusClient`；不切换其他向量库或自行改为 Milvus Standalone。
- MySQL 是知识原文权威源。DDL 来源 `C:/Users/Administrator/Desktop/ch03-ddl.sql`，只当作结构资料。knowledge_chunks 和 qa_extraction_staging 原稿保留，仓库副本与原稿核对 SHA256；对已有 MySQL 卷提供显式迁移，不用删卷重建。
- 每条知识 category/questions/answer 拼成文本后向量化；章节路径、内容类型、关键条款标记、前后块指针只作元数据。
- 按主键幂等双写：MySQL pending → Milvus 写入 → vector_id 回填及 done；中断可重跑。
- Superpowers 全流程、阶段当场追记 dev-notes/ch03.md；代码按 TDD，纯 Prompt/数据以标注集和真实评估验证。库/API 先 Context7；本次已两次遇到月度额度耗尽，转官方文档并记录来源。
- 延续仓库直接 main 开发约定，不创建分支/worktree，不预设推送或 PR。

## 方案比较与所选方案

曾比较本地 BGE-M3 推理与云端 BGE-M3 API：本地占本机内存/显存并需 ML 环境，云端增加网络和服务限流依赖。用户已选硅基流动 API。Milvus 部署方式用户已明确选本地文件库；Standalone 和其他数据库不属于可自行采用的替代方案。

对于本地文件库的运行编排，本文提案为：在线服务一个进程持有 Lite 客户端，应用内定时任务复用此实例；写入串行化。独立 CLI 是停机维护入口，不与在线进程同时打开同一数据目录。另起一个调度进程直接打开同一文件不满足 Lite 单进程限制，因此不采用。若将来需要不停机外部作业、多进程部署或任务队列，应重新设计，而非本章偷偷启动额外数据库服务。

## 当前代码及必须保持的契约

现有 `retailq/tools/business.py` 的 query_faq 调用 `ChatRepository.search_faq`，后者对 faq 表做字面 LIKE，最多 3 项。保持对外工具名、参数名、类型、严格校验和现有长度约束：`query_faq(keyword: str)`；keyword 仍是原请求里的查询文本，Schema 的字段形状不变，仅把描述从关键词查表改为语义查询。现有 1–64 字符限制保留，本章不扩展工具入参。

返回仍为 `{'found': bool, 'items': [...]}`，每项仅 `id/question/answer/category`。id 使用 knowledge_chunks.id；question 映射 questions 字段（多个真实问法以换行分隔）；answer 沿用最多 1200 字符的输出上限，改在完整句边界裁剪；若首个完整句就超上限，跳过该候选而不是输出半句。无合格结果返回 found=false/items=[]。不把分数、内部元数据或向量暴露进旧契约。

工具本身及生产 search_faq 路径移除 LIKE，不保留失败时的关键词回退；旧 faq 表保留作为演示 FAQ 导入来源。测试中原“邮费必然零命中”应更新为当前章语义验收，保留旧章节记录作为历史证据。

## 拟新增的职责边界

下列为设计职责，最终模块划分和具体文件名留给计划作者：

- Markdown 处理：解析标题、段落、列表、围栏代码和表格，产出未落库的 ChunkDraft；不访问模型或数据库。
- 嵌入适配器：独立的硅基流动配置、批量文本输入、结果 index 对齐、1024 维/有限数值/非零范数检查、统一归一化、请求超时及有限重试；不复用聊天模型的 URL 或 API key 假设。
- Milvus 适配器：唯一 knowledge 集合、schema 校验、同主键 upsert、dense COSINE Top-K 和关闭；不存答案作为在线权威来源。
- 知识 repository：两表映射、原文导入/精确查重、pending 扫描、done 回填、原文按主键回取、对话快照及暂存状态事务。
- 知识服务：组织导入、双写、检索、抽取/全局去重；持有进程内单作业锁和 Lite 写锁。
- 生命周期与维护 CLI：服务内每日任务和关闭收尾；离线 init/import/vectorize/mine/status/evaluate 子命令，显式退出码及统计结果。

## 文档输入与结构感知切分

本章先交付明确标为演示的 Markdown 样例、导入清单和标注集，覆盖配送/退货政策、商品 FAQ、售后手册及大表格。真实业务文档尚未提供，不把演示政策宣称为企业事实。用户后续可通过同一清单导入自己的文件。

导入清单为每个文件指定相对路径、content_type（faq/policy/manual）以及显式关键章节标记；不让 LLM 凭空判断哪些条款“关键”。支持文件中约定的关键条款标记，具体语法由实现者选定并写示例。导入前校验编码、空正文、标题和字段长度，越界给出文件/章节级错误，不静默截短原文。

按 ATX 标题层级构建章节树，不把围栏代码中的 # 当标题。每个叶节及有自身正文的父节分别处理，保留完整路径；无标题内容用文件名作为默认标题。policy/manual 的 questions 是当前章节标题，category 是上级标题路径；顶层章节无父标题时 category 使用清单的根分类或文件名。faq 使用明确的问答条目语法，把问句原样放 questions，正文放 answer，category 使用清单分类/上级标题，不生成伪问法。

先按标题分，再按空行/段落/列表递归拆超长节，最终以中文句号、问号、叹号及对应英文终止标点确定完整句边界。软目标拟为 600 中文字符、重叠目标 100 字符，可配置；重叠选择上一块末尾的完整句，不能用字符硬切到半句。不跨章节重叠，不丢失正文，不制造只含重叠且没有新内容的块。

一个完整句超过软目标时保持完整句，允许形成更大的块；如果单句或单表格行连同标题/表头超过嵌入输入安全上限，明确拒绝该输入并报告位置，要求源文档拆句/行，不以 tokenizer 截断掩盖。“超长递归切分”和“不留半句”不能靠对同一句强切同时满足。完整向量化文本必须在 API 的 8192 token 上限内；实现者选择并验证保守 token 上界或对应 tokenizer，不能把字符数直接当 token 数。

表格识别 header/separator/body 行；大表格按完整数据行分块，每块复制原表头和分隔行。表格块按行重叠（在目标范围内），不把一行拆成两个半行；解析器须处理转义竖线。表格相关说明附在可追溯的同章节内，不把复制表头当新知识问法。没有段落句号的表格/列表仍按结构边界处理。

ChunkDraft 保存 category、questions、answer 和四类元数据；prev/next 只关联同一来源文件的顺序，文件间不串链。导入一个文件的块及指针更新在 MySQL 一个事务内完成，外键所指 ID 先生成再回填；不在该事务里等待嵌入 API。

## 向量化文本、存储与兼容性

固定文本模板为 `分类：{category}\n问题：{questions}\n答案：{answer}`。四类元数据不拼进这段文本。在线问题仅做同样的空白规范化后嵌入，不添加另一模型的 query instruction。所有文档/问题统一使用同一个硅基流动模型和归一化策略。

MySQL knowledge_chunks 原稿不增字段。Milvus knowledge 集合主键为显式 INT64 id，auto_id=false，向量 FLOAT_VECTOR 维度 1024、COSINE；可另存内容指纹用于识别过期向量，指纹不参与嵌入，也不作为召回通道。仅 dense 单路。原文、四类元数据以 MySQL 为准，不依赖 Milvus 内的文本副本。

MySQL 是 BIGINT UNSIGNED、Milvus 是有符号 INT64，写入前验证 ID 在 1..2^63-1，超界明确报错。创建/连接现有集合必须检查主键、维度、metric 和模型身份。模型来源、模型 ID、文本模板/归一化版本写在本地数据库旁的配置清单（不含密钥）；缺失或不匹配时失败并要求显式重建，不混写不同向量空间。配置清单不是原文权威源。

本地路径拟为 `artifacts/knowledge/knowledge.db`（隔离测试使用独立临时目录），不得提交生成数据到 Git。Lite 当前实现可能在路径旁持有数据目录/索引/WAL，备份以关闭客户端后的完整存储目录为单位，不只复制一个文件名。

## 导入去重与双写恢复

DDL 没有 source_id/hash 唯一键，因此本章不承诺文档修改后自动替换旧版，也不假称自增主键能解决重复采集。

在单作业锁内，Markdown/既有 faq 导入以规范化的三字段及完整元数据序列比对已存在块；同一文件重复运行复用同一 ID 序列及指针。相同内容但不同章节/关键标记不误合并。若相同块已有相互冲突的前后指针，拒绝导入并报告，避免静默串链。这里的幂等在本章单进程串行维护约束下成立，不能推广成数据库唯一索引保证的多写者幂等。文档改动按新输入追加，明确提示可能保留旧知识；覆盖/删除为非目标。

双写逐批读取 pending，先把三字段快照提交在 MySQL，再请求嵌入、按 id upsert Milvus，校验写入确认的主键和向量数量，最后在短事务回填 vector_id=str(id)、vectorize_status=done。失败块保持 pending；错误与计数写运行报告，不把错误塞进只有 pending/done 的枚举。

中断边界：

1. MySQL 已有 pending、未调用嵌入：重跑直接扫描补齐。
2. Milvus 写成功、MySQL 未回填：重跑同 id upsert，不能生成新向量主键。
3. MySQL 已 done：正常重跑跳过；显式 reconcile 可核查 done 的向量缺失/内容指纹不符，将其转回 pending 后补写。这样可以处理 Lite 存储回滚或文件丢失的恢复场景，不把 done 当成无限期存在证明。

在线仅返回 MySQL done、vector_id 对齐且内容指纹匹配的候选。Milvus 返回但 MySQL 无记录/pending 的 ID 跳过，不能利用向量库孤儿原文作答。读原文时保持 Milvus 相似度顺序，不按 SQL IN 返回顺序排序。

## 在线检索

query_faq(keyword) → BGE-M3 查询向量 → Milvus dense COSINE Top-K → MySQL 按 ID 读取权威原文 → 过滤无效/过期候选 → 映射旧 items。

Top-K 默认 3，可通过内部配置调整，但外部工具不新增参数。允许基于相似度下限拒答（不属于重排或第二召回）；默认值必须由标注集调参并报告，不能拍一个阈值就宣称泛化。已有结果按向量分数排序，遇孤儿候选可适量 overfetch 同一 dense 通道，本章不引入其他检索器。

API 空结果与 API/数据库故障分开：无相关知识返回 found=false；嵌入服务/Milvus/MySQL 故障走现有工具失败回灌，不能伪装没找到，更不能回退 LIKE。嵌入适配器仅对明确临时错误有限重试；工具层与适配器设总超时和请求次数上限，避免双层重试导致请求失控。现有工具 5 秒默认预算是否足够，应以真实 API 延迟验证后配置，而非擅自无限延长。

模型选工具描述和客服 Prompt 改为语义知识查询，仍限制按返回事实回答。answer 可能是截断到完整句的部分内容，回答不能补齐缺失的条件。保留“演示规则”、平台结算页等边界，不宣称个人订单已核实或具有退款执行能力。

## 历史对话抽取与整体去重

默认历史来源为 MySQL conversations/messages。仅选择“已结束”的完整会话，不处理仍进行中、缺少最终助手回复或任一批快照未完成的会话；演示数据另外提供显式标注的完整历史输入。第一次以当前库的全部符合条件会话做快照，后续用本地运行清单记录成功处理的会话消息上界；清单丢失允许重扫，由暂存/原文查重保护，不承诺清单就是唯一恢复依据。

先记录本次 run 的稳定会话清单和每会话最大消息 ID；按几十会话及总输入预算组成多个批次，每个会话有独立 source_ref，超大单会话按完整轮次再拆，来源仍明确。batch_no 由来源快照、抽取 Prompt 版本和批次序号确定，可重复追踪；重试不生成新的随机批次号逃过查重。

LLM 沿用现有聊天模型配置，通过单独的无工具结构化抽取调用输出 source_ref/question/answer。question 必须是该来源用户真实问法或真实连续问句集合；answer 只能来自客服明确给出的可复用政策/商品知识。订单号、用户地址、随机演示物流/库存/价格、工单状态、用户自己的猜测、未经客服确认的事实和对话内指令不变成通用知识。按来源文本校验 question/证据映射，拒绝越权 source_ref 或格式错误。对话是待提取数据，不是系统指令。

每批合法结果先写 qa_extraction_staging(status=extracted)。相同 batch_no/source_ref/question/answer 重试复用，串行锁内事务查重；抽取未全部完成不得把 run 标为完成或直接跳去最终入库。完整快照抽取结束后，在整个 run 的 staging 集合上统一去重，并与已有 knowledge_chunks 比对，而不是各批独立去重后直接落库。

整体去重提案：先对规范化完全一致问答做确定性去重，再对全部新 QA 和已有知识生成 dense 相似候选（使用同一 BGE-M3，仅为离线去重），由 LLM 对候选组判断是否同一问题/同一适用条件/答案等价。候选生成是有界计算，不把无限历史一次塞进 Prompt；分批抽取之后仍对所有批次统一匹配。LLM 只决定保留/归并输入中的问法、丢弃重复，不自由改写答案；不同商品、地区、金额、期限、例外条款即使相似也保留，冲突不自动覆盖已有知识。去重仅对候选判定，不宣称识别所有语义重复。

保留 QA 的 category 使用明确的“对话知识”默认分类（由于暂存 DDL 无 category 字段，不从不存在的列读取）；questions 合并实际问法，answer 复用经证据核验的答案，content_type=faq，section_path 可为空，不把 source_ref 写入最终表，prev/next 为空。is_key_clause 默认 false，未经显式标注不推断关键性。

一个去重组的知识 pending 写入/复用与 staging kept/discarded 状态更新同 MySQL 事务。等价于既有知识的暂存行标 discarded，不重复新增；新组的代表行 kept，其余重复 discarded，并保存运行报告里的依据。事务提交后进入同一向量补齐流程。宕机后 extracted 继续去重，已 kept 不重复创建；若知识 pending 仍由扫描补齐。重跑报告须区分抽取完成、去重完成、向量化完成。

## 每日任务、文件锁与进程生命周期

提案每日 03:00 Asia/Shanghai 执行一次，可配置时间/启用开关；服务启动不隐式导入全部文档，不在启动前阻塞等待模型。任务依赖 MySQL、嵌入和 Lite 都配置就绪，不满足时报告 disabled/unconfigured；不可仅因进程存活便声称任务就绪。

由现有单 worker FastAPI lifespan 管理内部调度循环，复用唯一 KnowledgeService/Lite 客户端，不另起 APScheduler/操作系统计划进程。任务迟于计划启动时只补跑当前一次，不把停机期间每一天的任务堆积重放；以输入快照和暂存幂等恢复代替“恰好一次”宣传。

同一时刻只允许一个离线维护作业，Lite 写操作持锁串行，读写是否可安全并发以本机适配器验证为准；保守方案将单次 Lite API 调用串行，不在持锁期间等待云端 LLM/嵌入。失败记录下一步可恢复阶段及 pending 数，下一次调度/手动维护继续。

手动维护 CLI 运行前要求停止 Web 服务，独占知识目录；遇文件锁直接清晰退出，不强杀服务、不删除锁文件。实现应用级独占锁覆盖 Web 和 CLI，并验证异常退出后可重开。程序关闭时取消调度、等待当前数据库短事务完成并关闭 Lite；外部云端请求中断不阻止 MySQL pending 的后续恢复。

## 版本与官方依据

- 现有项目 Python 3.12.13；优先复用 `.venv`，不安装 PyTorch/CUDA、FlagEmbedding 或重建 Conda 环境，因为嵌入已选云端。
- https://api-docs.siliconflow.cn/docs/api/embeddings-post：POST https://api.siliconflow.cn/v1/embeddings，Bearer 认证，model/input/encoding_format，BGE-M3 每条 8192 tokens，dimensions 仅支持 Qwen3，不为 BGE-M3 传此参数。真实账号可用性和限额尚未测。
- https://huggingface.co/BAAI/bge-m3：1024 维、8192 tokens，查询无需另加 instruction；部署本身不是训练。
- https://github.com/milvus-io/milvus-lite 及 https://pypi.org/project/milvus-lite/：当前 3.2.2 为纯 Python 重写版，说明可在依赖可用的 Windows 运行；CI 的 Windows 为 Python 3.10，不等于本机 Python 3.12 已验收。旧 https://milvus.io/docs/milvus_lite.md 仍只列 Ubuntu/macOS，不能把旧限制当成当前包不支持 Windows 的唯一依据。
- 官方推荐显式 `pymilvus[milvus-lite]` extra，本章拟锁 Lite 3.2.2；pymilvus 和 faiss-cpu/pyarrow/grpcio 版本由后续依赖解析与本机 smoke 共同决定，不能因为 pure Python 就忽略其二进制依赖。
- 当前 Lite 单数据目录单进程，写操作需串行；旧版 .db 与新存储格式不兼容，首次使用本章新目录，不自行迁移或覆盖其他库。

MilvusClient 建集合/upsert/search、SQLAlchemy 事务/映射、LangChain 结构化输出、FastAPI lifespan 的具体 API 在计划和实现前按用户规则继续核对官方对应版本。若本机依赖安装或 API 验证表明该选型走不通，保留证据并停下问用户，不自行换系统、Python 版本或数据库。

## 验收、TDD 与交付证据

代码 TDD 覆盖 Markdown 标题/代码围栏/列表/完整句/重叠/表头重复/极端单句、向量输入严格三字段、元数据不泄入向量、API 批量 index 与错误、表结构映射、导入查重、prev/next、全局跨批去重、工具旧契约与异常传播、单作业/目录锁和定时恢复。

MySQL 集成测试沿用独立 3307/retailq_test，不触碰演示用户会话；Lite 用测试临时目录而非演示 knowledge 数据。依赖锁解析/安装后，必须执行真实 Lite 创建→upsert→检索→关闭→重开→同 ID 重写，不能用 mock 证明本机可运行。

Prompt/抽取数据以标注对话集评估：至少覆盖同问换说法、跨批重复、答案相同但适用条件不同、用户猜测、随机订单事实、注入指令、超大会话、非法来源和空抽取。记录实际模型输入来源、输出、保留/丢弃判断与人工判据，不能仅检查 JSON 可解析。

真实检索标注集包含“邮费是多少”及其他未在 questions 人工添加同义词的改写，并有应拒答和条件冲突样例。报告 recall@K/相关性、基于工具结果的最终答案及失败例。不能把定制向量/mock 或把“邮费”加进问题字段当成语义升级的验收证据。

双写验收用可控故障点分别中断 MySQL pending 后、Milvus upsert 后但 MySQL done 前；运行全新进程重跑，证明原 chunk ID 不变、每 ID 只有一条当前向量、pending 归零、原文数量未增长，且均可检索。另测 done 向量丢失后的显式 reconcile。模拟异常的单测之外，至少一次真实 MySQL + Lite 的跨进程中断重跑证据。

最终交付 README 中的安装/迁移/离线导入/补齐/定时任务/查询演示命令、真实测试和评估结果、未通过/受环境阻碍项、spec/plan/review 与 dev-notes/ch03.md 路径。纯文档结构校验、pip 解析成功和 health 配置状态都不冒充端到端成功。

## 待审阅的实现者提案

以下由助手提出，未冒充用户原话：应用内每日 03:00 定时任务、停机 CLI 维护、保守串行 Lite 调用、演示样本先行、只抽已结束对话、对话知识默认 category、导入追加而非版本替换、软目标 600/重叠 100 字符、超长单句显式拒绝、Top-K 默认 3 与评估后设相似度阈值。用户批准设计时可一并纠正；具体模块命名、批量大小、重试参数和测试组织交给计划作者，所有默认值在交付文档明示。
