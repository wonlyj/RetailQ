# 第六章：正式分流器与订单优先售后

入口仍为`/api/chat/stream`。先用既有聊天上游完成指代消解与必要口语归一，再输出严格的intent/confidence JSON；完整问题保留原文，无法唯一消解的订单不猜测。七类业务意图另有“其他”兜底；本章仅配置默认大模型，不做降级器、训练或跨会话记忆。

物流/订单继续有界Agent；商品咨询单查询知识；退款退货/售后进入确定性节点：确认或选择订单→query_order真实回执→执行侧规划→具体核心问题扩写2–4条→同快照policy检索→统一重排/生成前闸→无自由读工具的主力Agent。通用规则FAQ也先拿订单，但不扩写。主力Agent只根据本单事实与政策判断，缺商品状态等信息时必要澄清，不追问退款原因。

查询仅在检索侧临时使用；知识、chunk与向量不复制。policy_single/policy_multi各自绑定语料、模型、查询代码/Prompt身份及实际策略文件SHA。候选按chunk与来源版本合并，再对原独立问题一次重排。分数闸属于相关性启发式，不等于事实完备性或资格判定；独立强制检索的误放也如实保留在评估记录中。

缺单号使用LangGraph`interrupt()`与原AsyncSqliteSaver文件，`order_selector→paused`表示正常等待；`resume`通过`Command(resume=...)`继续同turn，不增加用户消息，不重跑消解/分类。剩余活动时间写入实际Interrupt.value，SQL是可对账的镜像；人类等待不扣活动时间，已耗时间/token/模型/工具额度不重置。公开卡片不含内部预算。

刷新读取`GET /api/conversations/{cid}/pending?user_id=...`恢复卡片；`POST /api/conversations/{cid}/turns/{turn_id}/resume`要求user_id/interrupt_id/request_key与order_id或cancel，归属与卡片候选由服务端校验。同key同选择幂等，换选择冲突；已完成请求只回放SQL结果。`GET .../turns/{turn_id}`供未知结果核查。当前暂停不能被新消息覆盖，取消后可以继续其他话题。终态事件在正常释放请求资源后发出。

订单与卡片共用固定演示目录1001、1002、001001，前导零保留，每个本地身份各自视图。商品/物流仍为原随机演示工具；这些不是实际电商订单接口或真实鉴权。

退款完整答复提供绑定本单的“申请退款”入口。固定原因：不想要了、商品质量问题、商品与描述不符、收到商品破损/缺件、未收到货、其他。表单提交使用专用refund端点，旧ticket端点不能绕过原因绑定。确认只创建`ticket_type=退款/status=待处理`工单，不执行资金退款；重复相同确认保持唯一工单，未知结果先GET核查。

启动、迁移与用户浏览器验收见[人工指南](ch06-manual-acceptance.md)；完整过程见[开发记录](../dev-notes/ch06.md)。
