# 第六章实施计划评审

日期：2026-11-09。

- 用户授权：“设计批准，继续计划评审”。
- 已批准设计：`docs/superpowers/specs/2026-11-09-ch06-router-design.md`。
- 计划：`docs/superpowers/plans/2026-11-09-ch06-router.md`。
- 审阅者：独立只读 `/root/ch06_plan_review`；先独立审读，再对作者修订复核。检查需求覆盖、依赖、接口/类型、可执行验证、恢复/幂等/预算和评估证据。
- 排除：前端实现与设计审查、前端测试要求；只核对其依赖的后端契约可用。没有修改产品代码、执行迁移、启动服务或调用真实模型。

## 首轮：Not Ready

0 Blocker，2 Important；其余需求覆盖、依赖、既定技术、核心路由工具限制、退款绑定与后端API契约未发现重大规划不一致。

| 编号 | 级别 | 发现 | 作者修订与验证要求 | 当前状态 |
| --- | --- | --- | --- | --- |
| P1 | Important | durable interrupt先于SQL保存，崩溃后无法精确得知剩余活动预算，可能恢复原额度 | T4在实际Interrupt.value内持久化pause_budget.remaining_active_seconds；reconcile只读该值，校验有限与上限，同interrupt不增加SQL较小值；公开API过滤内部预算。T5要求真实SQLite跨进程：先用90/120秒，checkpoint后SQL前退出，新进程恢复≤30秒，长人类等待不扣/不加额度，重放节点不覆盖原预算。 | 同一审阅者复核关闭 |
| P2 | Important | gateway只返回解析值与usage，现有内部解析会丢raw及畸形completion，无法实现计划承诺的真实JSON分母 | T1定义每请求async RouterObserver，call_id绑定预算/阶段、case或TurnRef；raw在任何格式/finish校验前落文件或SQL，随后同ID记录parsed/invalid/transport_error，不使用共享gateway全局可变回调。T8要求三条completion有一条畸形时原文三条齐全、parse rate=2/3，解析失败行继续采集未完成SQL证据。 | 同一审阅者复核关闭 |

## 作者补充自审修订

- 固定resolver输出cap=256与支持较长完整问题存在冲突；T1加入动态保守cap与resolve_output_cap，T5在start_turn/流响应前preflight，预算容不下显式422且零外呼/用户写入。加入长完整问题和不足预算测试要求。
- 核对旧工具测试路径为`tests/test_tools.py`，人工指南是T7创建、T8修改，清除不确定文件名。
- 文档/源码/框架读取与规划检查不等同于产品测试通过；每个测试、真实评估与迁移步骤仍未执行。

## 修订复核：Ready

同一审阅者读取修订计划，原文结论：

> Ready. Both Important findings are closed in the revised plan.

复核确认预算元数据与跨进程测试、请求隔离observer与错误分母证据契约已闭合，动态输出预算/preflight也解决长问题截断规划缺口；变更部分没有剩余重大契约不一致。

本结论仅代表计划可进入实施。用户尚需审阅计划并确认本章执行方式；尚未完成任何功能实现、功能测试、真实模型评估、浏览器验收、代码review或finish。
