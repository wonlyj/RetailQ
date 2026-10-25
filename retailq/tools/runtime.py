import asyncio
from dataclasses import dataclass
import json
from typing import Literal

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from retailq.config import Settings
from retailq.errors import ServiceError


@dataclass(frozen=True)
class ToolContext:
    conversation_id: int
    user_id: str
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    payload: dict
    receipt_state: Literal['unrecorded','persisted','pending'] = 'unrecorded'


def failure(code: str, message: str, state='unrecorded') -> ToolOutcome:
    return ToolOutcome({'ok':False,'error':{'code':code,'message':message}}, state)


class ToolRegistry:
    def __init__(self, settings: Settings, repository=None, context: ToolContext | None = None):
        self.settings = settings
        self.repository = repository
        self.context = context
        self._tools: dict[str, tuple[BaseTool, bool]] = {}
        self.pending: set[asyncio.Task] = set()

    @property
    def tools(self) -> list[BaseTool]:
        return [entry[0] for entry in self._tools.values()]

    def register(self, tool: BaseTool, *, retry_safe: bool) -> None:
        if tool.name in self._tools:
            raise ValueError('duplicate tool')
        self._tools[tool.name] = tool, retry_safe

    async def _invoke(self, tool, arguments, write: bool):
        try:
            result = await tool.ainvoke(arguments)
            return ToolOutcome({'ok':True,'data':result}, 'persisted' if write else 'unrecorded')
        except Exception:
            if not write:
                raise
            outcome = failure('tool_execution_failed','工单创建结果未确认，请稍后核查。')
            try:
                await asyncio.to_thread(self.repository.append_message, self.context.conversation_id,
                    self.context.user_id,'tool',json.dumps(outcome.payload,ensure_ascii=False),
                    None,self.context.tool_call_id)
                return ToolOutcome(outcome.payload,'persisted')
            except Exception:
                return outcome

    async def execute(self, call: dict) -> ToolOutcome:
        entry = self._tools.get(call.get('name'))
        if entry is None:
            return failure('unknown_tool','未注册该工具，未执行操作。')
        tool, retry_safe = entry
        try:
            arguments = tool.args_schema.model_validate(call.get('args')).model_dump()
        except (ValidationError, TypeError, ValueError):
            return failure('invalid_arguments','工具参数不符合要求，未执行操作。')
        write = tool.name == 'create_ticket'
        attempts = 1 + (self.settings.tool_read_retries if retry_safe else 0)
        for attempt in range(attempts):
            task = asyncio.create_task(self._invoke(tool, arguments, write))
            if write:
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
            try:
                return await asyncio.wait_for(asyncio.shield(task) if write else task,
                                              self.settings.tool_timeout_seconds)
            except TimeoutError:
                if write:
                    return failure('outcome_unknown','工单操作等待超时，结果尚未确认，请勿重复提交。','pending')
                if attempt + 1 == attempts:
                    return failure('tool_timeout','查询工具超时，请稍后重试。')
            except asyncio.CancelledError:
                # A write keeps running under shield and owns its durable receipt.
                raise
            except Exception as exc:
                transient = isinstance(exc, (ConnectionError, OSError)) or (
                    isinstance(exc,ServiceError) and exc.code == 'database_unavailable')
                if not transient or attempt + 1 == attempts:
                    return failure('tool_execution_failed','工具执行未成功，请稍后重试。')
            await asyncio.sleep(0.05)
        raise AssertionError('unreachable')

    async def drain(self):
        if self.pending:
            await asyncio.gather(*list(self.pending),return_exceptions=True)


def build_registry(repository, context: ToolContext, settings: Settings) -> ToolRegistry:
    from retailq.tools.business import business_tools
    registry = ToolRegistry(settings, repository, context)
    for tool in business_tools(repository, context):
        registry.register(tool,retry_safe=tool.name not in ('create_ticket','query_faq'))
    return registry
