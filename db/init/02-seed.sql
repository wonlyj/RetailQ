SET NAMES utf8mb4;
INSERT INTO faq (id, question, answer, category) VALUES
  (1, '退货政策是什么', '演示规则：签收后7天内，商品完好且不影响二次销售，可申请退货；具体资格和费用以平台售后规则为准。', '售后'),
  (2, '运费标准是什么', '演示规则：普通配送订单满99元免运费，不满99元收取8元运费；偏远地区及特殊商品以平台结算页为准。', '配送'),
  (3, '如何开具发票', '演示规则：可在订单详情申请电子发票；具体操作以平台页面为准。', '发票');
INSERT INTO conversations (id, user_id, status) VALUES (1, 'seed-demo-user', '已转人工');
INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id) VALUES
  (1, 'user', '演示：订单1001迟迟未到，请帮我创建投诉工单。', NULL, NULL),
  (1, 'assistant', NULL, JSON_ARRAY(JSON_OBJECT('id','seed_call_1','type','function','function',JSON_OBJECT('name','create_ticket','arguments','{"description":"演示订单1001配送延迟","ticket_type":"投诉"}'))), NULL),
  (1, 'tool', '{"ok":true,"data":{"ticket_no":"TSEED20261024001","ticket_type":"投诉","status":"待处理"}}', NULL, 'seed_call_1'),
  (1, 'assistant', '演示工单 TSEED20261024001 已创建，当前待处理。', NULL, NULL);
INSERT INTO tickets (ticket_no, conversation_id, description, ticket_type) VALUES
  ('TSEED20261024001', 1, '演示订单1001配送延迟', '投诉');
