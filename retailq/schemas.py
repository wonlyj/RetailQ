from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=8000)]
RequestType = Literal["refund", "return_refund", "exchange", "repair", "logistics", "other", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: Text


class ChatRequest(StrictModel):
    message: Text
    history: list[HistoryMessage] = Field(default_factory=list, max_length=100)
    conversation_id: int | None = Field(default=None, strict=True, gt=0, le=18446744073709551615)
    user_id: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64)] = 'local-demo'

    @model_validator(mode="after")
    def complete_turns(self):
        if self.conversation_id is not None and self.history:
            raise ValueError('server conversation ID and imported history cannot be combined')
        if len(self.history) % 2 or any(
            message.role != ("user" if i % 2 == 0 else "assistant")
            for i, message in enumerate(self.history)
        ):
            raise ValueError("history must contain alternating, completed user/assistant turns")
        return self


class ExtractRequest(StrictModel):
    description: Text


class AfterSalesResult(StrictModel):
    """从用户原文提取的售后信息，不能推断或执行订单处理。"""

    order_id: str | None = Field(description="明确给出的唯一订单号，保留前导零；缺失或多订单不明确时为 null")
    request_type: RequestType = Field(description="refund/return_refund/exchange/repair/logistics/other/unknown；不明确或冲突为 unknown")
    expected_solution: str | None = Field(description="用户明确期望的方案，缺失为 null，不推断赔偿或执行结果")
