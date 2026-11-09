"""Fixed per-identity views for local demos, not an ecommerce order database."""
from copy import deepcopy
from retailq.errors import ServiceError

_ORDERS=(
    {'order_id':'1001','product_name':'保温杯','amount':89.0,'status':'已完成','ordered_at':'2026-11-01',
     'received_at':'2026-11-04','goods_condition':None},
    {'order_id':'1002','product_name':'运动鞋','amount':199.0,'status':'已发货','ordered_at':'2026-11-06',
     'received_at':None,'goods_condition':None},
    {'order_id':'001001','product_name':'日常双肩包','amount':129.0,'status':'待发货','ordered_at':'2026-11-08',
     'received_at':None,'goods_condition':None},
)


class DemoOrderProvider:
    def list_orders(self,user_id):
        if not isinstance(user_id,str) or not user_id:
            raise ServiceError('order_identity_missing',422,'请选择有效的本地演示身份。')
        return tuple({**deepcopy(row),'source':'demo','demo_owner':user_id,'is_custom':None} for row in _ORDERS)

    def get_order(self,user_id,order_id):
        row=next((r for r in self.list_orders(user_id) if r['order_id']==order_id),None)
        if row is None:raise ServiceError('order_not_found',404,'该订单不在当前演示订单目录中，请从订单卡片选择。')
        return row
