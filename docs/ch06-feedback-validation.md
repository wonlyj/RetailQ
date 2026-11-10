# 第六章人工E/H/O反馈修复验证

日期：2026-11-10。对应补丁起点1f60943，后端无新增组件/模型，前端无代码改动。原首次交付的P/M/E报告保留为历史，不能代表本补丁。

## 用户确认的交付范围

用户先报告E/H拒答及O无选择器，随后纠正：“我记错了，确实弹出来了选择器，但是选择后出现了问题”。三项实际均为政策相关性误拒。扩展验证发现单一重排分数无法兼顾所有领域后，用户明确选择：**“保持单一分数闸，交付本次修复并明确限制”**。因此不新增结构化语义闸，不承诺所有政策问题均可回答。

## 修复及浏览器结果

根因：正确条款已召回，但带订单号和选单措辞的原问题被直接用于全局重排，相关性分数偏低。核心扩写首条现在是保留实质条件的标准政策问法，用于一次全局重排；原问题仍参与召回并交给Agent，订单绑定不被改写。首条复制原问题明确失败，只有非首条副本可去重，防止侧面问法被提升并丢条件。FAQ仍单查询、不扩写。

完整退款答复增加历史体积后，E第三轮曾在第二次Agent决策前超输入预算。现在只裁剪完整旧轮，保留系统规则、当前独立问题、当前所有工具请求/回执及证据；不加预算，当前轮自身放不下时仍limited。R46曾把尾部System拼入问题，附加指令改并入首System，最后输入保持Human；重新真实评估通过。

独立IAB实际观察：

| 人工项 | 实际观察 |
| --- | --- |
| E | 1001物流→“它可以退吗”正确关联1001、引用退货政策并说明未知条件、展示申请退款；再切物流正常完成，没有知识拒答或limited |
| H | 1002尺码不合适走售后，引用收到7天内/完好配件/同款换货条款，缺收货日期及商品状态则澄清，不保证资格 |
| O | 否定1001后三张卡片等待；实际点击1002后同轮继续，答复1002运动鞋及退货政策条件，没有沿用1001或知识拒答 |

本次只实测上述路径及一次取消；不冒称其余15项GUI全部验收。未提交真实退款资金。前端沿用用户Vibe例外，未做前端TDD/代码审查。

## 当前证据

| 项目 | 结果/路径 |
| --- | --- |
| 后端受影响两组 | **71 passed**、**73 passed**；`artifacts/verification/ch06/manual-feedback-20261110/{review-green,final-integration}.log`。本次不将历史816项当当前全量 |
| 冻结Prompt | **58/58作者语义通过、157/157原始JSON可解析，review退出0**；`artifacts/evals/ch06/prompts-20261110-feedback-b/report.json` |
| 新增定制/生鲜/非质量条件扩写 | **6/6语义通过**，首条保留否定、签收天数、未拆封及商品类别；`artifacts/verification/ch06/manual-feedback-20261110/constraints-report.json`。只验证扩写，不等于订单资格验证 |
| 正式端点完整采集 | **14场景、21轮**，逐条语义**20通过/1失败**；E/H/O对应W01/W04/W13通过，所有原始SQL/checkpoint/恢复证据通过。`artifacts/evals/ch06/workflow-20261110-feedback-b/report.json` |
| 校准 | 原24条重新检索并逐条重读来源，SHA绑定标注；`artifacts/evals/ch06/calibration-20261110-feedback-b/` |
| 发布策略 | `artifacts/evals/ch06/policies-20261110-feedback-b/`；由校准数据拟合，单查询阈值0.045776758342981346，多查询0.14746519923210147 |
| 原16条已见回归 | **13/16：2误放、1误拒**；`artifacts/evals/ch06/policy-regression-20261110-feedback-b/report.json` |
| 全新16条留出 | **14/16：2误放、0误拒**；`artifacts/evals/ch06/policy-holdout-20261110-feedback-b/report.json`。改拟合规则前冻结，SHA256：12F1E765CF16678246C5BA1418C453516A579A0D0A3AB9FABC082AF0899F3534；未用于调参 |

21轮中W12第一轮的金额129及前导零001001均正确，但无检索证据时多出了`[1]`标记，作者判语义FAIL、列为延期次要项；该标记不代表存在来源。故完整workflow的review**退出1**，不是整套PASS。其余轮次（含一次取消）按各自预期审阅；正式收集HTTP成功不代替语义判断。

## 保留限制与评审处置

唯一独立补丁review：0 Critical/2 Important/0 Minor，固定范围1f60943..13d12ae。I1已红→绿修复。I2的校准边界规则已改为：最高balanced accuracy、相同实际误放率时取留有正例召回裕量的最低严格分区边界；没有按已见test分数手填阈值。A的13/16及所有失败原文保留。原16已用于问题定位，B只称回归，不能再称独立测试；新留出16结果未用于继续调整。

B仍误拒“退回商品缺少配件是否影响退货”，并误放两种mode的港口滞箱费；新留出也误放两种mode的另一港口问题。正确条款分数可以低于无关条款，分数不等于语义适用性或资格。用户已选择保持此方案；**I2剩余广泛验收未达标的风险由明确裁定保留，不冒称消除所有误拒/误放。** 正式Other分类另行拒绝非零售领域，但分类若错，底层仍可能误放。

延期次要项：本次W12无证据的多余引用标记；原章M1极小预算流前预留不足、M2部分故障/旧退款HTTP边界测试覆盖不足继续保留。本次不宣称长进程资源稳定性或生产一般化通过。评审详见[反馈review](superpowers/reviews/2026-11-10-ch06-feedback-review.md)。

## 重测

按[更新的人工指南](ch06-manual-acceptance.md)在原窗口Ctrl+C，重新执行窗口A整段命令，加载B策略并重启worker；新建对话优先测E/H/O。不能复用未重启的旧worker，也不能把旧拒答气泡当新版本重算结果。

```powershell
# 已人工审阅的固定报告：第一个应0；第二个因W12孤立引用明确为1
.\.venv\Scripts\python.exe -m scripts.evaluate_router review --report artifacts/evals/ch06/prompts-20261110-feedback-b/report.json
.\.venv\Scripts\python.exe -m scripts.evaluate_router review --report artifacts/evals/ch06/workflow-20261110-feedback-b/report.json
```

所有A/B报告、旧策略与失败日志保留。本机artifacts不随Git发布；新机器需要实际重新采集和校准，不能用空文件替代策略。
