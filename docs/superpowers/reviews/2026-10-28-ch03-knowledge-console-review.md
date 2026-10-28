# 知识审核工作台：整体审查与处置

审查范围：87a3660b6999a23cdd4d1c50794ea4d3dd9a2450..d1a3dd5b9fceb1c79db3aaadd31771592faa7b98。

方法：Native计划末尾一次新上下文gpt-6-astra审查；前次调用仅额度失败、无结论，本次才是实际审查。不修改checkout/SQL、不调用云端、不派二次审查。批准spec/plan、五项Review Focus及全部执行Ruling均已阅读。原始审查包和验证日志保留在本计划scratch，finish前归档。

结论（修复前）：无Critical，4项Important、4项Minor，With fixes；尚不能按本地功能完整交付结案。两套既有完整测试均384通过，但没有覆盖下面四种故障。

## 已确认的重要问题

| 编号 | 原始位置 | 问题与实际影响 | 处置 |
| --- | --- | --- | --- |
| I1 / P1 | review_store.py:131、runtime.py:79 | 单个operations JSON读取失败中断list_runs和runtime构造，甚至已封存run也不能解除阻塞，用户失去核查/封存入口 | 已RED→GREEN；prepare/已应用决定/未知retry三种重开及文件身份回归；坏文件保留，可信关联或独立故障槽明确封存 |
| I2 / P2 | review_preparation.py:162 | processed_sources仅信applied审计，没有核对当前SQL；目标或成员漂移后仍禁选，封存后无法重选 | 已RED→GREEN；目标与成员两种SQL漂移；水位要求仓储确证，零QA完成独立保留 |
| I3 / P2 | web/knowledge.js:84、156 | 旧GET轮询或旧POST响应可以清掉/覆盖新seal的请求键、busy及轮询状态 | 已RED→GREEN；实际发布JS模块4种受控响应交错，旧响应不能改新意图 |
| I4 / P2 | app.py:47 | runtime初始化异常直接终止lifespan，未实现静态页可打开、管理API明确503的设计；不能用LIKE回退掩盖错误 | 已RED→GREEN；实际lifespan初始化异常后静态200、管理503、原health三字段及FAQ不可用不变 |

## Minor与作者重新分级

| 编号 | 审查原始级别 | 问题 | 作者判断 |
| --- | --- | --- | --- |
| M1 | P3 | README把关闭知识时的LIKE历史基线写成当前行为；实际无faq_search返回503 | 提升为Important并随I4纠正：直接误述固定dense单路及功能可用性，可能引导用户错误配置；不改变现有工具合同 |
| M2 | P3 | 分类报告只在全部完成后写盘，第二例KeyboardInterrupt会丢掉已获取的第一例真实结果 | 提升为Important：长云端评估中断会丢失已付费取得的原始结果，须逐例原子留存；保留未完成状态，不能判通过 |
| M3 | P3 | GET采集器HTTP500也返回2，没有按约定用1区分执行失败；报告确实保留500状态 | deferred：无假成功或结果丢失，暂通过报告requests判断故障 |
| M4 | P3 | live恢复预检未完整复用is_embedding_configured，空白key/空endpoint会进入子进程后返回场景失败1，未在预检返回2 | deferred：错误报告仍保留且不换模型；用户需先核对配置。空SecretStr本身truthiness为False，已实测排除“空SecretStr总为真”的误疑 |

## 优点与覆盖边界

原子组记录把决定/prepared/applied放一起；SQL应用与恢复核对全部成员/目标指纹，维护租约持续至实际线程退出，持久命令/语义别名/独立封存、严格字符串BIGINT/同源API/DOM文字渲染都有针对性覆盖。本轮阅读完整代码，并用生产函数的纯内存重现确认I1–I4及M2；未重新执行数据库测试。审查前证据为当前384 passed/205.61s、dev锁384 passed/208.77s、DDL批准哈希一致。

## Declined to judge与作者Ruling

1. 真实云端抽取、语义去重、分类及BGE质量：按用户要求留其运行；代价是本地协议通过不能保证线上语义质量。
2. live BGE kill恢复及实际代理/供应商可用性：没有新的live证据；代价是网络/真实嵌入恢复仍待验收，不切换服务商。
3. 用户跳过的B3定时人工验收：保持未测，仅确认本地scheduler门禁；代价是不能声称人工定时链路通过。
4. Desktop原始DDL现时双文件比对：原件缺失，仅仓库对照批准哈希；代价是须有原件才能重新双向核验。
5. 公网多用户认证、多worker/分布式与恶意高权限本机篡改真实性：保持本机单owner范围，普通损坏仍须修复；代价是不适合作公网服务或密码学审计。
6. 断电/硬盘损坏/不成套备份恢复的跨资源强一致：保持设计明确不承诺，仍验进程中断和原子写失败；代价是不能扩展为任意灾难恢复保证。
7. 基点以前所有实现的独立完整审查：本次只覆盖新增功能及兼容边界；代价是不替代此前未完成的全章审查。

## 修复与最终验证

作者已完成一次集中修复，不派第二次独立复审：

- M1文案与实际FAQ不可用合同核对纠正；M2用checkpoint回归及真实CLI中断替身回归先RED，再验证初始清单/逐例原子写入，第一例结果保留且部分报告pending。
- 修复后当前环境 **397 passed / 215.41s**，既有dev锁环境 **397 passed / 215.97s**；三个pip check、runtime必要导入、git diff --check通过。仓库DDL仍匹配批准哈希；桌面原稿缺失的限制不变。
- 实际浏览器补验未知操作故障槽：明确显示无法确证归属，人工封存后current_run_id=null、原文仍3条、坏文件原字节保留。证据artifacts/verification/ch03-review/browser-reviewed-fault-20261028-01/。fixture正常lifespan退出，无测试服务残留。
- Node VM测试API的Context7核对次序发生偏差：先写后查，已如实记录；补查官方Node24 createContext/SourceTextModule/SyntheticModule/link/evaluate/namespace与experimental flag，并在本机24.16.0核验接口，无API版本返工或依赖安装。

作者最终处置：本地实现与存储协议门禁达到交付条件，重要项没有未修复项；M3/M4仍明确延期。原独立审查的With fixes结论保留，以上为作者回归证明，不能冒称第二位reviewer复审通过。真实模型与业务语义验收继续pending，用户B3仍跳过。
