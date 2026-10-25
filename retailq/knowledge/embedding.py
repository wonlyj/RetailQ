"""SiliconFlow dense BGE-M3. No implicit provider or keyword fallback."""
import math
import time

import httpx

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.knowledge.token_budget import BGETokenBudget


def unavailable():
    return ServiceError('embedding_unavailable',503,'知识嵌入服务未成功，请稍后重试。')


def invalid():
    return ServiceError('embedding_invalid_response',502,'知识嵌入服务返回的数据无效，请核查模型配置。')


class SiliconFlowEmbedding:
    def __init__(self,settings: Settings,budget: BGETokenBudget,*,transport: httpx.BaseTransport | None=None):
        if not settings.is_embedding_configured:
            raise ServiceError('embedding_not_configured',503,'请在本地配置知识嵌入服务。')
        self.settings,self.budget=settings,budget
        self.http=httpx.Client(timeout=settings.embedding_timeout_seconds,trust_env=False,transport=transport,
            headers={'Authorization':'Bearer '+settings.embedding_api_key.get_secret_value()})

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
    def close(self) -> None: self.http.close()

    def embed(self,texts: list[str],*,deadline: float | None=None) -> list[list[float]]:
        for text in texts: self.budget.validate(text)
        result=[]
        size=self.settings.embedding_batch_size
        for offset in range(0,len(texts),size):
            result.extend(self._batch(texts[offset:offset+size],deadline))
        return result

    def _batch(self,texts,deadline):
        end=deadline if deadline is not None else time.monotonic()+self.settings.embedding_timeout_seconds*(1+self.settings.embedding_retries)
        for attempt in range(1+self.settings.embedding_retries):
            remaining=end-time.monotonic()
            if remaining<=0: raise unavailable()
            response=None
            try:
                response=self.http.post(self.settings.embedding_base_url.rstrip('/')+'/embeddings',
                    json={'model':self.settings.embedding_model,'input':texts,'encoding_format':'float'},
                    timeout=min(remaining,self.settings.embedding_timeout_seconds))
                if time.monotonic()>=end: raise unavailable()
                if response.is_success:
                    return self._parse(response,len(texts))
                transient=response.status_code in (429,502,503,504)
            except httpx.RequestError:
                transient=True
            if not transient or attempt==self.settings.embedding_retries: raise unavailable()
            try:
                delay=float(response.headers.get('Retry-After','0.1')) if response is not None else 0.1
            except ValueError:
                delay=0.1
            if not math.isfinite(delay) or delay<0 or delay>=end-time.monotonic(): raise unavailable()
            time.sleep(delay)
        raise unavailable()

    def _parse(self,response,count):
        try:
            payload=response.json()
            if payload['model']!=self.settings.embedding_model: raise ValueError()
            rows=payload['data']
            if not isinstance(rows,list) or len(rows)!=count: raise ValueError()
            ordered={}
            for row in rows:
                index=row['index'];vector=row['embedding']
                if type(index) is not int or not 0<=index<count or index in ordered: raise ValueError()
                if not isinstance(vector,list) or len(vector)!=1024: raise ValueError()
                if any(type(v) not in (int,float) or not math.isfinite(v) for v in vector): raise ValueError()
                norm=math.hypot(*vector)
                if not math.isfinite(norm) or norm<=0: raise ValueError()
                ordered[index]=[v/norm for v in vector]
            return [ordered[i] for i in range(count)]
        except (ValueError,TypeError,KeyError,OverflowError):
            raise invalid() from None
