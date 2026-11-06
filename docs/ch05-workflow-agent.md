# 第五章：确定性Workflow与有界Agent

主入口`/api/chat/stream`由LangGraph编排，指代节点暂时原样透传，分类Prompt严格返回七类JSON。路由在代码中固定：商品咨询/退款退货→强制retrieve→gate→Agent；物流/订单/售后→Agent；投诉→固定安抚与两个选项；闲聊→固定话术。闲聊仍有一次分类请求。

Agent在图内交替执行`agent_decide→tools`，直到独立`answer`节点流式输出。裸循环位于`retailq/workflow/agent.py:run_bare_agent`，先用`python -m scripts.demo_agent_loop --mode controlled`查看同一决策/工具/答复逻辑不依赖图的版本。controlled模型是明确标注的模拟；`--mode real`使用已配置服务。

工具只复用第二章的query_order、query_product、query_logistics；模型申请和实际执行都限制白名单。create_ticket不属于Agent工具。知识类复用第四章混合检索及校准策略，gate记录实际策略字节SHA。低分在生成前固定兜底并落低置信池；配置故障不混入该池。相关性门槛不等于完备的事实充分性判断，正式置信度升级仍留后续章节。

默认每轮最多4次工具、6次Agent模型调用（含最终答复）、120秒、65536总token；意图与检索归一预留也计入整轮账本。provider usage存在时用实耗，否则保留保守预留；日志不会把预留标成实耗。输入预算包含历史、工具schema、证据和当前阶段消息；重复调用、预算或时限到达会停止。最终答复单独分阶段，控制JSON不混入用户正文。

State贯穿图，AsyncSqliteSaver用独立文件与数据库身份绑定，使用同步durability。MySQL保存消息、工具回执、低置信问题和workflow_turns/actions元数据；每轮从已提交SQL事实构造上下文。恢复持锁对账SQL遗留active（含首checkpoint前退出），保留已确定失败原因，丢弃旧图续跑点开启新轮，不重放工具和半截回答。用户动作事实独立于断流的模型role graph，并从已提交回执恢复。流取消等待短SQL写线程和后台工作实际结束后才释放租约；绝对整轮deadline传入检索，不允许过期后启动新外部请求。

投诉或Agent只能建议handoff/ticket；选项与原完整助手消息绑定。handoff仅在浏览器确认后模拟两句人工接入。ticket独立确认后POST动作API，调用原工单工具写tickets；工单不再把会话状态改为“已转人工”。相同动作/内容幂等，改变内容冲突，未知结果仅GET核查，不自动重试。

新增SSE `workflow`包含节点enter/exit，`actions`包含建议与来源；工具徽章仍来自tool_status。sources使用workflow-turn绑定的归档接口，旧工具来源API保留。最终消息SQL提交与图checkpoint成功后才发actions/done；客户端固定meta身份并要求恰好一帧有效actions（可为空），缺帧或错位不标完整。token此前已流式显示，结构性引用失败返回error且不伪报完成。

完整启动、显式迁移、双worker、浏览器验收与演示命令见[人工测试指南](ch05-manual-acceptance.md)，评估边界见[评估记录](ch05-evaluation.md)。本机身份、随机业务数据、SQLite与模拟人工仍是演示边界，尚不构成公网生产部署条件。
