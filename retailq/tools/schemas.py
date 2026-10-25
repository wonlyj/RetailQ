from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class OrderInput(ToolInput):
    order_id: Identifier = Field(description='用户提供的订单号，字符串，保留前导零')


class ProductInput(ToolInput):
    product_id: Identifier = Field(description='用户提供的商品编号，字符串')


class FAQInput(ToolInput):
    keyword: Identifier = Field(description='用户原话中的问题或相关短语，不添加用户未提供的条件')


class TicketInput(ToolInput):
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)] = Field(description='用户说明的问题事实和人工处理诉求')
    ticket_type: Literal['售后', '投诉', '咨询'] = Field(description='工单类型')
