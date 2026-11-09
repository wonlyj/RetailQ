from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from retailq.tools.schemas import Identifier

REFUND_REASONS=('不想要了','商品质量问题','商品与描述不符','收到商品破损/缺件','未收到货','其他')


class RefundRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    user_id: Identifier
    reason: Literal['不想要了','商品质量问题','商品与描述不符','收到商品破损/缺件','未收到货','其他']
    request_key: str=Field(pattern=r'^[0-9a-f]{32}$')


def refund_description(order_id,reason):
    return f'退款申请；订单号：{order_id}；退款原因：{reason}。'
