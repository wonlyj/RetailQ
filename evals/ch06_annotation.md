# 第六章冻结语义样例

ch06_router_cases.jsonl含48条逐轮问题，八类每类至少3条，四段多轮分别覆盖1001/1002话题切换、双单歧义、001001前导零与否定。

intent为主要当前诉求；verbatim=true要求逐字透传。expected_resolution为作者标注的语义参照，不要求非verbatim问题与参照逐字相同；必须逐条检查指代唯一性、数字/否定/时间/对象保真，不可新增用户未给出的商品状态。双单歧义保留歧义，不能任选一单。core_case仅用于核心路由扩写规划，纯通用规则为false。

作者审阅需填写report.cases[].review.status=pass/fail及具体reason；HTTP成功、JSON解析成功不能单独证明语义通过。失败更改Prompt后换新目录运行，不改冻结标签迎合结果。真实模型结果与受控协议测试分别报告；模型配置名不证明供应商真实部署身份。
