from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class OrderInput(ToolInput):
    order_id: Identifier = Field(description='用户提供的订单号，字符串，保留前导零')


class ProductInput(ToolInput):
    product_id: Identifier = Field(description='用户提供的商品编号，字符串')


class FAQInput(ToolInput):
    keyword: Identifier = Field(description='用户原话中的问题或相关短语，不添加用户未提供的条件')
    category: Identifier | None = Field(default=None, description='用户明确指定的品类')
    category_prefix: Identifier | None = Field(default=None, description='用户明确指定的品类层级')
    content_type: Literal['faq', 'policy', 'manual'] | None = None

    @model_validator(mode='after')
    def one_category_filter(self):
        if self.category is not None and self.category_prefix is not None:
            raise ValueError('category and category_prefix are mutually exclusive')
        return self


class TicketInput(ToolInput):
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)] = Field(description='用户说明的问题事实和人工处理诉求')
    ticket_type: Literal['售后', '投诉', '咨询'] = Field(description='工单类型')
