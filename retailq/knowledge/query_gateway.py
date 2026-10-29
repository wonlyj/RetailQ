"""Single-turn query rewrite via the configured chat-completions endpoint."""
from pathlib import Path
import time
import httpx

from retailq.errors import ServiceError


class QueryGateway:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.prompt = (Path(__file__).resolve().parents[1] / 'prompts' / 'query_normalize.txt').read_text(encoding='utf-8')
        self.http = httpx.Client(trust_env=False, transport=transport,
                                 headers={'Authorization':'Bearer '+settings.openai_api_key.get_secret_value()})

    def normalize(self, question, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ServiceError('knowledge_query_timeout',504,'知识查询等待超时，请稍后重试。')
        try:
            response = self.http.post(self.settings.openai_base_url.rstrip('/')+'/chat/completions',
                json={'model':self.settings.openai_model, 'messages':[{'role':'system','content':self.prompt},
                      {'role':'user','content':question}], 'temperature':0, 'max_tokens':256},
                timeout=min(remaining,self.settings.model_timeout_seconds))
            response.raise_for_status()
            choice = response.json()['choices'][0]
            content = choice['message']['content']
            if choice['finish_reason'] != 'stop' or not isinstance(content, str) or choice['message'].get('tool_calls'):
                raise ValueError('incomplete normalization')
            return content.strip()
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            raise ServiceError('knowledge_query_understanding_failed',503,'问题归一服务未成功，请稍后重试。') from None

    def close(self): self.http.close()
