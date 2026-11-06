# 第五章 Native 全量代码评审

独立只读审查者：`/root/ch05_final_review`，GPT-6 Astra high，fresh context；起点2077c90，范围含T1–T7提交与T8工作树/新文件。按requesting-code-review模板审查，未派实现代理、未开第二评审席，未操作数据库或运行整套测试。

初评：**Ready to merge? No.** 确定性路径、工具双白名单、动作幂等、SQL原子提交及真实子进程恢复覆盖合理；以下问题必须先修。

| 编号 | 分级 | 位置 | 发现及影响 |
| --- | --- | --- | --- |
| R1 | Critical | workflow/graph.py log、agent.py execute、service.py release | 裸asyncio.to_thread SQL写入在取消后仍运行，租约可能先释放，旧助手答复跨过新用户消息。审查者用真实图+阻塞假finalize复现。 |
| R2 | Important | workflow/repository.py load_projection | 断流缺回执的普通role graph会吞掉之后用户从旧按钮建票的事实；已提交成功回执而action仍executing时也丢编号。 |
| R3 | Important | workflow/service.py prepare、agent.py、evidence.py gate | 旧历史占满窗口时未给阶段指令/证据留空间，短的新业务问题也直接limited或输入超限。保持原完整轮裁剪，补必需开销。 |
| R4 | Important | web/app.js、scripts/client.py | 缺actions帧仍当完整回复；sources可改写meta轮次；恢复后消息会话ID与验证会话不一致。 |
| R5 | Important | scripts/evaluate_workflow.py summarize/run | 最小伪证据+自报expected/布尔值即可通过；分类采集未复用正式分类入口。应冻结SQL内容/摘要/回执并离线复算约束，无需每次连接3308。 |
| R6 | Important | workflow/service.py prepare、runtime.py rebase、repository.py mark_terminal | 首checkpoint前失败或未消费流可留active；恢复可能覆盖已确定failed原因。持锁按SQL对账，保留终态。 |
| R7 | Important | workflow/evidence.py retrieve、knowledge/runtime.py、agent.py execute | 未传整轮绝对截止时间，后台检索到期后仍可启动新请求；裸循环工具缺剩余整轮限制。 |
| R8 | Important | scripts/evaluate_workflow.py run | 18805端口不证明服务绑定3308，错连3306时先写才发现；首次聊天前需只读核对服务端数据库身份。 |

Minor（暂缓）：应用日志仅有节点退出，失败日志缺当前阶段/预算来源，故障定位信息不足（graph.py wrapper、service.py error）。SSE节点进入事件存在，功能不依赖新增日志，本次不扩大修复。

Declined to judge：真实GUI留用户验收；全量pytest由主代理负责；供应商真实模型身份、生产负载、真人接入未评。作者接受这些边界，不能据此声称已通过。

处理：作者接受R1–R8分级，进入一次TDD修复，通过覆盖测试及完整绿测关闭；Native不派二审。逐项红绿和最终结果将在完成时追记。

## 作者的一次修复及定向证据

| 问题 | 修复与先红后绿证据 |
| --- | --- |
| R1 | 统一write_sql，取消后等待实际线程结束再清理。`test_cancelled_graph_sql_keeps_lease_until_actual_thread_exit`使用真实图与两个Coordinator实例验证竞争锁；Agent申请/回执两种阻塞另测。 |
| R2 | 动作事实独立组装，保留原消息/动作/确认描述，成功回执优先于lagging action状态；`test_old_action_fact_survives_later_incomplete_tool_turn`两个参数场景。 |
| R3 | 原完整轮裁剪计算阶段/schema开销，检索后为至少一份必要证据继续丢旧轮；`test_long_history_drops_old_turns_for_current_stage_and_evidence`业务/知识两例300轮。 |
| R4 | meta身份固定，恰好一帧actions，sources/动作消息ID一致，恢复会话一致；native stream-contract和Python五种协议场景。最终可用标签中文映射另有回归。 |
| R5 | v2采集正式分类器、冻结SQL内容/摘要/申请回执及票据来源，离线重算标注与约束；薄证据/自改标签红→绿，19项评估器测试含受控正例及6种证据破坏。受控正例不是真实模型证据。 |
| R6 | 持锁按SQL对账遗留active；prepare失败/未消费流/提交期间取消均标终态；已确定失败不覆盖；对应repository/recovery与new_turn_commit回归。 |
| R7 | 知识retrieve接受整轮绝对deadline，各层在新请求前检查；裸工具按剩余时间执行；slow_snapshot、deadline传播、bare工具时限回归。 |
| R8 | 本机只读库摘要端点，采集模型与聊天前核对3308身份；错误服务不发业务请求，端点与collector两个回归。 |

扩大相关回归159 passed，最终独立锁环境100 passed、pip check通过。真实G轮v2正式分类21/21、12/12结构与逐事实语义审阅通过；129项文件摘要与当前可执行文件相符。最终完整suite **757 passed、0 skipped**（CH04_FAULT_DB=1，684.45秒）。

作者关闭结论：R1–R8均有先红后绿覆盖并通过整套回归，阻塞项关闭；日志细化Minor保留暂缓，GUI实际点击待用户。按Native流程未进行二审，初评No与修复证据均保留，不将作者的测试关闭写成独立审查者复核通过。

后续人工验收：2026-11-06用户反馈已按指南执行完成、没有报错，并授权推送远端main；GUI待反馈项据此关闭，证据来源为用户反馈，不改变以上独立代码审查的历史范围和结论。
