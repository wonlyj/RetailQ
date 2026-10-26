"""Reject model-native tool syntax without interpreting or executing it."""
from retailq.errors import ServiceError


_TOOL_MARKERS = tuple(
    f'<{closing}{separator}DSML{separator}'
    for separator in ('｜｜','｜','||','|')
    for closing in ('','/')
)
_PARTIAL_MARKERS = frozenset(
    marker[:length]
    for marker in _TOOL_MARKERS
    for length in range(1,len(marker))
)
_MAX_PENDING = max(map(len,_PARTIAL_MARKERS))


def contains_tool_markup(text: str) -> bool:
    return any(marker in text for marker in _TOOL_MARKERS)


def require_plain_model_text(text: str) -> None:
    if contains_tool_markup(text):
        raise ServiceError('unsupported_tool_protocol',502,
            '模型返回了未解析的工具标记，答复未完成，请核查模型服务的工具调用协议。')


class ModelTextBuffer:
    """Hold only a possible marker prefix, so split SSE tokens cannot leak it."""
    def __init__(self):
        self.pending = ''

    def push(self,text: str) -> str:
        combined = self.pending+text
        require_plain_model_text(combined)
        held = max((length for length in range(1,min(len(combined),_MAX_PENDING)+1)
                    if combined[-length:] in _PARTIAL_MARKERS),default=0)
        if held:
            self.pending = combined[-held:]
            return combined[:-held]
        self.pending = ''
        return combined

    def finish(self) -> str:
        result,self.pending = self.pending,''
        return result
