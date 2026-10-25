import json
import math
import time

import httpx
import pytest

from retailq.config import Settings
from retailq.errors import ServiceError


class Budget:
    def validate(self,text):
        if not text or len(text)>100: raise ValueError('input too long')


def client(handler,retries=1):
    from retailq.knowledge.embedding import SiliconFlowEmbedding
    settings=Settings(_env_file=None,embedding_api_key='embedding-test-key',openai_api_key='chat-key',embedding_retries=retries)
    return SiliconFlowEmbedding(settings,Budget(),transport=httpx.MockTransport(handler))


def payload(vectors):
    return {'model':'BAAI/bge-m3','data':[{'index':i,'embedding':v} for i,v in enumerate(vectors)]}


def test_request_has_only_bge_parameters():
    def handler(request):
        assert str(request.url)=='https://api.siliconflow.cn/v1/embeddings'
        assert request.headers['Authorization']=='Bearer embedding-test-key'
        assert json.loads(request.content)=={'model':'BAAI/bge-m3','input':['知识'],'encoding_format':'float'}
        return httpx.Response(200,json=payload([[3.0]+[0.0]*1023]))
    with client(handler) as adapter:
        assert adapter.embed(['知识'])[0][0]==1.0


def test_reverse_indices_restore_input_order():
    p=payload([[3.0]+[0.0]*1023,[0.0,4.0]+[0.0]*1022]);p['data'].reverse()
    with client(lambda _:httpx.Response(200,json=p)) as adapter:
        rows=adapter.embed(['一','二'])
        assert rows[0][0]==1 and rows[1][1]==1


@pytest.mark.parametrize('indices',[[0,0],[0,2],[0],[-1,1],[False,1]])
def test_missing_duplicate_or_out_of_range_index_fails(indices):
    p=payload([[1.0]+[0.0]*1023 for _ in indices])
    for row,i in zip(p['data'],indices):row['index']=i
    with client(lambda _:httpx.Response(200,json=p)) as adapter:
        with pytest.raises(ServiceError): adapter.embed(['一','二'])


@pytest.mark.parametrize('vector',[[1.0]*3,[0.0]*1024,[True]+[0.0]*1023,['x']+[0.0]*1023,[1e308]*1024])
def test_wrong_dimension_nan_inf_zero_and_empty_fail(vector):
    with client(lambda _:httpx.Response(200,json=payload([vector]))) as adapter:
        with pytest.raises(ServiceError): adapter.embed(['知识'])
        assert adapter.embed([])==[]
        with pytest.raises(ValueError):adapter.embed([''])
        with pytest.raises(ValueError):adapter.embed(['x'*101])


def test_nonfinite_json_vectors_are_rejected():
    for value in ['NaN','Infinity']:
        body='{"model":"BAAI/bge-m3","data":[{"index":0,"embedding":['+value+','+','.join(['0']*1023)+']} ]}'
        with client(lambda _:httpx.Response(200,text=body)) as adapter:
            with pytest.raises(ServiceError):adapter.embed(['知识'])


def test_retry_budget_and_authentication_no_retry():
    for status,expected in [(401,1),(403,1),(400,1),(429,2),(503,2)]:
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(status,text='server-secret')
        with client(handler) as adapter:
            with pytest.raises(ServiceError) as error: adapter.embed(['知识'])
            assert len(calls)==expected
            assert 'secret' not in str(error.value)
            count=len(calls)
            with pytest.raises(ServiceError): adapter.embed(['知识'],deadline=time.monotonic()-1)
            assert len(calls)==count


def test_model_mismatch_and_retry_after_beyond_deadline():
    p=payload([[1.0]+[0.0]*1023]);p['model']='different-model'
    with client(lambda _:httpx.Response(200,json=p)) as adapter:
        with pytest.raises(ServiceError):adapter.embed(['知识'])
    calls=[]
    def slow(request):
        calls.append(1)
        return httpx.Response(429,headers={'Retry-After':'99'})
    with client(slow) as adapter:
        with pytest.raises(ServiceError):adapter.embed(['知识'],deadline=time.monotonic()+0.2)
    assert calls==[1]


def test_explicit_http_proxy_routes_without_resolving_destination():
    from http.server import BaseHTTPRequestHandler,HTTPServer
    import threading
    from retailq.knowledge.embedding import SiliconFlowEmbedding
    received=[]
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body=json.dumps(payload([[1.0]+[0.0]*1023])).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=HTTPServer(('127.0.0.1',0),Proxy)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        settings=Settings(_env_file=None,embedding_api_key='test',embedding_base_url='http://embedding.invalid/v1',embedding_proxy=f'http://127.0.0.1:{server.server_port}',embedding_retries=0)
        with SiliconFlowEmbedding(settings,Budget()) as adapter:
            assert adapter.embed(['知识'])[0][0]==1
        assert received[0][0]=='http://embedding.invalid/v1/embeddings'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_proxy_credentials_are_hidden():
    settings=Settings(_env_file=None,embedding_proxy='http://user:proxy-credential@127.0.0.1:7890')
    assert 'proxy-credential' not in repr(settings)
