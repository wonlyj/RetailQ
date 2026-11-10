# 第六章验证记录

日期：2026-11-10。Native实施，默认沿用配置中的`deepseek-flash`聊天上游；模型名是配置标识，不证明供应商实际部署身份。前端按用户例外直接Vibe Coding，浏览器点击由用户按[指南](ch06-manual-acceptance.md)执行，**GUI验收待反馈**。

## 最终真实模型与正式端点

| 验证 | 结果 | 本地证据 |
| --- | --- | --- |
| Prompt冻结48原样例+10范围/指代边界 | **58/58作者语义审阅通过；157/157控制JSON可解析；review退出0** | `artifacts/evals/ch06/prompts-20261110-p/report.json` |
| 双worker正式聊天/resume HTTP | **14场景、21轮，原始证据及作者语义审阅通过；review退出0** | `artifacts/evals/ch06/workflow-20261110-m/report.json` |
| 政策校准 | 24条，两种mode分别拟合，实际来源条款人工标注；未用test调阈值 | `artifacts/evals/ch06/calibration-20261110-e/` |
| 独立强制政策检索 | **14/16，未全通过**；每个mode各误放一条海运集装箱滞箱费问题，无误拒 | `artifacts/evals/ch06/policy-test-20261110-e/report.json` |
| 生效策略 | policy_single / policy_multi各自独立身份及实际文件SHA | `artifacts/evals/ch06/policies-20261110-e/` |

最终P/M报告绑定当前可执行代码与Prompt字节。报告保留冻结输入、原始completion、finish_reason、tool_calls、逐阶段调用ID、实际SQL与同turn checkpoint、来源归档及SHA。评估器从原始事实复算，不信任phase/ok/actual标签；拒绝缺例、改标签、串轮、会话/用户归属不符、无实际订单回执、未提交答复和伪恢复。作者逐条审阅独立问题、意图、扩写与最终政策答复，HTTP200及JSON可解析本身不构成语义通过。

W01完成物流→退款→物流，退款轮补全1001，先拿订单、扩写和强制检索；W01/W04/W13因政策低分保守拒答，**这三例不证明资格判断已完成**。W02真实interrupt后选择1001，同turn继续至政策/Agent和退款入口；同原因确认两次，SQL只有一个待处理退款单。W05首轮实际串行查询1001/1002，之后因歧义选1002；七个业务轮次均验证当前轮真实订单回执。W10取消后切物流；W12保留001001前导零；W13/W14否定旧单或切商品时不误绑历史单。合计**5次选单恢复、1次取消**，没有新增用户消息冒充恢复。

政策闸14/16是已知限制：集装箱滞箱费会与零售运输破损条款产生较高相关分。正式意图Prompt将非零售金融/旅行/地产/港口费用归“其他”，范围holdout及W06/W11验证该边界且零业务工具/政策调用。**分类若误路由，底层分数闸仍可能误放。** 原计划“全部负例拒绝”未达标，作为作者ruling和独立review接受的演示限制保留，不改冻结test标签或阈值，不宣称生产泛化达标。

Agent只按实际检索条款条件化判断，证据不足时明确限定覆盖；完整来源链接不表示整份文档都进入模型证据。服务日期与用户时间陈述冲突时要求核实，不凭常识追加包装/吊牌条件。扩写中的政策搜索分支不是订单事实，不能写回原问题。订单目录固定，物流和商品工具仍是随机演示数据；退款提交仅创建待处理工单，不执行资金操作。

## 测试、评审与运行限制

| 验证 | 实际结果 |
| --- | --- |
| 冻结全量清单，8节点一进程串行执行 | **816项，0 failure/error/skip**；102批JUnit与清单在`artifacts/verification/ch06/full-20261110-final/` |
| 最后适配后的网关/Agent/协议/评估受影响集 | **89 passed**，含新增真实HTTP供应商报文测试 |
| 最后日期/业务回执证明受影响集 | **41 passed**，含新增逐单回执反例 |
| 最终聊天、恢复、退款、迁移等集成集 | **78 passed in 60.37s**；`artifacts/verification/ch06/native-20261110-final/integrated-delivery2.log` |
| 独立锁定Python3.12环境 | 核心69 passed、1个MySQL用例为避免并发fixture显式deselected；另网关/协议16 passed；两个环境pip check均无冲突 |
| 前端 | 三个ES模块语法解析通过；无浏览器点击通过声明 |

当前总收集818项：完整816清单之后增加两项，均已在上述受影响集中执行。**没有一次完整818项单进程通过的结果**，也不将重叠子集相加成测试总数。全量包含隔离3309故障数据库用例；未删除具名卷或清理3306。最后集成验证前既有容器停止，恢复3307/3308及Milvus后78项通过；服务不可用的等待不记成产品失败或通过。

首轮26 failed/766 passed/4 skipped的受控上游/历史路由断言已按新接口修复，未重标第五章冻结报告。随后单进程多次出现Windows`0xC0000409`、MemoryError及明确OpenBLAS分配失败；单例旧审核断连及32项审核API独立通过，但未知根因保留。按[OpenBLAS官方说明](https://www.openmathlib.org/OpenBLAS/docs/runtime_variables/)限`OPENBLAS_NUM_THREADS=1`和`OMP_NUM_THREADS=1`，使用冻结清单分批；**分批通过不证明长进程累积资源稳定性已修复**，未改系统页面文件或依赖版本。

唯一独立后端review为0 Critical/3 Important/2 Minor；I1错误选单、I2原始completion信任、I3冻结输入/会话归属证明均先红后绿修复，真实P/M复验通过；没有第二次review。保留M1极小预算流前完整预留不足、M2中途deadline/语料切换及旧退款HTTP非法字段零写入覆盖不足，详见[评审记录](superpowers/reviews/2026-11-09-ch06-code-review.md)。开始前超时/单路失败/服务层绕过测试不能冒充上述缺失覆盖。

## 重现

先按[人工指南](ch06-manual-acceptance.md)启动3308/19531、迁移并启动18806双worker；其中提供4条curl演示及15项GUI检查。`prompts`和`run`必须使用全新目录：

```powershell
$ProjectPython = '.\.venv\Scripts\python.exe'
$RunDir = 'artifacts/evals/ch06/recheck-' + (Get-Date -Format yyyyMMdd-HHmmss)
& $ProjectPython -m scripts.evaluate_router prompts --output ($RunDir + '-prompts')
& $ProjectPython -m scripts.evaluate_router run --base-url http://127.0.0.1:18806 --output ($RunDir + '-workflow')
& $ProjectPython -m scripts.evaluate_router review --report ($RunDir + '-workflow/report.json')

# 检查已完成且已人工审阅的固定报告
& $ProjectPython -m scripts.evaluate_router review --report artifacts/evals/ch06/prompts-20261110-p/report.json
& $ProjectPython -m scripts.evaluate_router review --report artifacts/evals/ch06/workflow-20261110-m/report.json
```

新采集尚无作者逐条语义reason时退出2待审；失败退出1；证据与语义均通过才退出0。历史A–O等采集包括失败、诊断及被后续代码身份取代的报告，全部保留，不能当作当前通过。Native过程、原始失败与修复日志归档于`artifacts/verification/ch06/native-20261110-final/`；本机artifacts不随Git分发，跨机复现需重新采集。
