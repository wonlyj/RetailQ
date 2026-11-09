# 第六章验证记录

日期：2026-11-09。Native实施，默认沿用配置中的`deepseek-flash`聊天上游；模型名是配置标识，不证明供应商实际部署身份。前端按用户例外直接Vibe Coding，浏览器点击由用户按[指南](ch06-manual-acceptance.md)执行，目前待反馈。

## 真实模型与正式端点

| 验证 | 结果 | 本地证据 |
| --- | --- | --- |
| Prompt冻结48原样例+8范围holdout | 56/56意图及作者语义审阅通过；149/149控制JSON可解析 | `artifacts/evals/ch06/prompts-20261109-g/report.json` |
| 双worker正式聊天/resume HTTP | 12场景、17轮通过；作者逐轮审语义，SQL/checkpoint/归档/工单事实复算通过 | `artifacts/evals/ch06/workflow-20261109-d/report.json` |
| 政策校准 | 24条，两个mode分别拟合，来源实际条款人工标注；未用test调阈值 | `artifacts/evals/ch06/calibration-20261109-b/` |
| 独立强制政策检索 | **14/16，未全通过**；每个mode各误放一条海运集装箱滞箱费问题，无误拒 | `artifacts/evals/ch06/policy-test-20261109-b/report.json` |
| 生效策略 | policy_single / policy_multi各自独立身份及实际文件SHA | `artifacts/evals/ch06/policies-20261109-b/` |

`review`命令真实退出0；只把HTTP200、布尔ok或自动标签正确当成通过是不够的。报告记录冻结输入、原始completion、finish_reason、tool_calls、逐阶段调用ID、实际SQL与同turn checkpoint、来源归档以及代码/Prompt SHA。评估器拒绝缺例、改标签、串轮、未提交答复、伪恢复和当前代码manifest不符；历史失败报告保留，不能在新代码上冒充当前通过。

W01实测完成物流→退款→物流，退款轮正确补全1001并先拿订单、扩写和强制政策检索，但分数0.0903低于校准门槛，因此拒答，未进入Agent。W04尺码换货同样因低分拒答。W02无上下文出现真实interrupt，选择1001后同turn继续并产生可查政策和退款入口；相同原因确认两次只有一个待处理退款单。W05先串行查两个订单，再遇歧义弹卡选1002；W10取消后正常切物流；W12保留001001前导零。正式端点本轮包含3次选单恢复和1次取消，均未新增用户消息冒充恢复。

政策闸的14/16是已知限制：集装箱滞箱费会与零售运输破损条款产生较高相关分，分数不能代替领域判断。正式意图Prompt明确非零售金融/旅行/地产/港口费用归“其他”；8条范围holdout和W06/W11端点验证该边界且零业务工具/政策调用。**如果意图模型将范围外问题误归业务意图，底层分数闸仍可能误放；本章未建立生产泛化保证。** 不调整冻结test标签、不开新组件、不声称阈值本身已修复。强制检索benchmark未满足原计划的全部负例拒绝标准，作为作者ruling保留，供最终后端review判断。

本轮语料只检索到有限片段时，回答明确限定覆盖范围；可点击的来源文档是完整归档，不表示整份文档都作为Agent证据。演示签收日期与用户提供的时间条件可能冲突，不能将该固定小样例集准确率理解成订单资格的一般保证。订单目录固定，物流和商品工具仍生成随机演示数据，退款提交只是工单，不执行资金操作。

## 测试与故障恢复

新增测试先红后绿覆盖严格协议、同快照合并/重排、共享deadline、策略身份、实际SQLite中断、跨进程先checkpoint后SQL退出、≤30秒已耗预算恢复、两个运行时并发恢复、旧卡片/完成回放、退款旧端点防绕过与原子唯一工单。Prompt/数据按用户要求使用标注集实际验证；前端只做三个ES模块语法解析，不新增前端TDD/审查。

首轮广泛回归26 failed/766 passed/4 skipped：受控上游缺resolve/置信度、新增模型调用计数、旧退款路径及历史第五章proof映射等逐项修复，受影响79项通过，未把旧冻结标签改成新标签。第二轮进程在旧审核断连测试附近以Windows本机异常码`0xC0000409`退出，没有完整pytest结论；开启faulthandler的全量C正在复验，最终结果待填。四个独立故障库测试使用既有3309库，未清理3306或删除具名卷。

最终独立后端review及集中修复结果待写入[评审记录](superpowers/reviews/2026-11-09-ch06-code-review.md)。前端明确排除。

## 重现

先按[人工指南](ch06-manual-acceptance.md)设置3308/19531环境、迁移并启动18806双worker。`prompts`与`run`输出必须为全新目录：

```powershell
$ProjectPython = '.\.venv\Scripts\python.exe'
$RunDir = 'artifacts/evals/ch06/recheck-' + (Get-Date -Format yyyyMMdd-HHmmss)
& $ProjectPython -m scripts.evaluate_router prompts --output ($RunDir + '-prompts')
& $ProjectPython -m scripts.evaluate_router run --base-url http://127.0.0.1:18806 --output ($RunDir + '-workflow')
& $ProjectPython -m scripts.evaluate_router review --report ($RunDir + '-workflow/report.json')
```

新采集尚无作者逐条语义审阅时退出2待审；失败退出1；只有证据及具体语义reason全部通过才退出0。原始事实不能为了通过而改写。历史A–F Prompt和A–C流程均保留，包括失败或被代码身份变化取代的报告。
