import json
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, PromptTemplate

from retailq.schemas import AfterSalesResult, ChatRequest

TEMPLATE_DIR = Path(__file__).with_suffix("")


def load_system_prompt() -> str:
    template = PromptTemplate.from_template((TEMPLATE_DIR / "customer_service.txt").read_text(encoding="utf-8"))
    return template.format(role_name="电商智能客服")


def build_chat_messages(request: ChatRequest, system_prompt: str) -> list[BaseMessage]:
    history = [
        (HumanMessage if item.role == "user" else AIMessage)(content=item.content)
        for item in request.history
    ]
    prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=system_prompt), MessagesPlaceholder("history"), ("human", "{message}"),
    ])
    return prompt.invoke({"history": history, "message": request.message}).to_messages()


def schema_json() -> str:
    return json.dumps(AfterSalesResult.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


def build_extract_messages(description: str) -> list[BaseMessage]:
    template = PromptTemplate.from_template((TEMPLATE_DIR / "after_sales.txt").read_text(encoding="utf-8"))
    return [SystemMessage(content=template.format(schema_json=schema_json())), HumanMessage(content=description)]
