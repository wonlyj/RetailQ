import asyncio
from datetime import datetime
import random

from langchain_core.tools import tool

from retailq.errors import ServiceError
from retailq.tools.schemas import FAQInput, OrderInput, ProductInput, TicketInput


def business_tools(repository, context):
    @tool(args_schema=OrderInput)
    async def query_order(order_id: str) -> dict:
        """用订单号查询固定演示订单；与聊天选单卡片共用目录，不是公司真实订单接口。"""
        from retailq.workflow.demo_orders import DemoOrderProvider
        return DemoOrderProvider().get_order(context.user_id,order_id)

    @tool(args_schema=ProductInput)
    async def query_product(product_id: str) -> dict:
        """用商品编号查询商品名称、价格和库存。内部随机生成演示数据，不是真实商品接口。"""
        return {'source':'demo', 'product_id':product_id, 'name':random.choice(['日常双肩包','运动鞋','保温杯']),
                'price':round(random.uniform(30,300),2), 'stock':random.randint(0,80)}

    @tool(args_schema=OrderInput)
    async def query_logistics(order_id: str) -> dict:
        """用订单号查询物流到哪里、配送进展。内部随机生成演示数据，不是真实物流接口。"""
        return {'source':'demo', 'order_id':order_id, 'status':random.choice(['运输中','派送中','已签收']),
                'location':random.choice(['上海转运中心','济南配送站','收件地址']),
                'updated_at':datetime.now().isoformat(timespec='seconds')}

    @tool(args_schema=FAQInput)
    async def query_faq(keyword: str, category: str | None = None, category_prefix: str | None = None, content_type: str | None = None) -> dict:
        """根据用户原话查询退货政策、配送费用、商品FAQ和售后知识，支持相近问法。只依据返回内容回答。"""
        if getattr(repository, 'rag_retrieve', None) is not None:
            if not context.raw_question: raise ServiceError('rag_context_missing', 409, '缺少当前用户问题。')
            if (category and category not in context.raw_question) or (category_prefix and category_prefix not in context.raw_question):
                raise ServiceError('rag_filter_unconfirmed', 422, '品类过滤条件未由用户明确提供。')
            type_words={'faq':('faq','常见问题'), 'policy':('policy','政策'), 'manual':('manual','手册')}
            if content_type and not any(word in context.raw_question.lower() for word in type_words[content_type]):
                content_type = None
            retrieve = getattr(repository, 'retrieve_knowledge', repository.rag_retrieve)
            result = await asyncio.to_thread(retrieve, context.raw_question, category=category,
                                             category_prefix=category_prefix, content_type=content_type)
            context.retrieval_result = result
            items = [{'id':c.id, 'question':c.draft.questions, 'answer':c.draft.answer, 'category':c.draft.category}
                     for c in result.candidates]
        else:
            if getattr(repository, 'rag_required', False):
                raise ServiceError('knowledge_not_configured',503,'知识查询服务不可用。')
            items = await asyncio.to_thread(repository.search_faq, keyword)
        return {'found':bool(items), 'items':items}

    @tool(args_schema=TicketInput)
    async def create_ticket(description: str, ticket_type: str) -> dict:
        """仅在用户前端独立确认后创建待处理工单；不改变会话转人工状态，不执行退款或即时接通人工。"""
        if not context.tool_call_id:
            raise ServiceError('missing_tool_request',409,'缺少工具申请。')
        return await asyncio.to_thread(repository.create_ticket, context.conversation_id, context.user_id,
                                       description, ticket_type, context.tool_call_id)

    return [query_order, query_product, query_logistics, query_faq, create_ticket]
