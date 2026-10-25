import json
import math
import os
from pathlib import Path
import threading
import time

from pymilvus import DataType, MilvusClient

from retailq.errors import ServiceError
from retailq.knowledge.contracts import SearchHit, VectorItem


def store_error():
    return ServiceError('knowledge_vector_unavailable',503,'知识向量库操作未成功，请稍后重试。')


class MilvusKnowledgeStore:
    def __init__(self,uri: Path,identity: dict[str,str]):
        self.uri=Path(uri).resolve()
        self.identity=dict(identity)
        self._lock=threading.Lock()
        self.client=None
        self.uri.parent.mkdir(parents=True,exist_ok=True)
        self.identity_path=self.uri.with_suffix('.identity.json')
        try:
            existing=json.loads(self.identity_path.read_text(encoding='utf-8')) if self.identity_path.exists() else None
            if existing is not None and existing!=self.identity:
                raise ServiceError('knowledge_identity_mismatch',409,'知识向量模型或预处理版本不一致，请显式核查重建。')
            self.client=MilvusClient(str(self.uri))
            if existing is None and self.client.list_collections():
                raise ServiceError('knowledge_identity_missing',409,'已有向量库缺少模型身份清单，请人工核查。')
            self.ensure_collection()
            if existing is None:
                temporary=self.identity_path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(self.identity,ensure_ascii=False,sort_keys=True),encoding='utf-8')
                os.replace(temporary,self.identity_path)
        except Exception:
            if self.client is not None:self.client.close()
            raise

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    def ensure_collection(self) -> None:
        with self._lock:
            if not self.client.has_collection('knowledge'):
                schema=MilvusClient.create_schema(auto_id=False,enable_dynamic_field=False)
                schema.add_field('id',DataType.INT64,is_primary=True)
                schema.add_field('embedding',DataType.FLOAT_VECTOR,dim=1024)
                schema.add_field('fingerprint',DataType.VARCHAR,max_length=64)
                indexes=MilvusClient.prepare_index_params()
                indexes.add_index(field_name='embedding',index_type='FLAT',metric_type='COSINE')
                self.client.create_collection('knowledge',schema=schema,index_params=indexes)
            schema=self.client.describe_collection('knowledge')
            fields={f['name']:f for f in schema['fields']}
            valid=set(fields)=={'id','embedding','fingerprint'} and not schema['auto_id'] and not schema.get('enable_dynamic_field')
            valid &= fields.get('id',{}).get('type')==DataType.INT64 and fields.get('id',{}).get('is_primary',False)
            valid &= fields.get('embedding',{}).get('type')==DataType.FLOAT_VECTOR and int(fields.get('embedding',{}).get('params',{}).get('dim',0))==1024
            valid &= fields.get('fingerprint',{}).get('type')==DataType.VARCHAR and int(fields.get('fingerprint',{}).get('params',{}).get('max_length',0))==64
            names=self.client.list_indexes('knowledge')
            valid &= any(self.client.describe_index('knowledge',name).get('metric_type')=='COSINE' and self.client.describe_index('knowledge',name).get('field_name')=='embedding' for name in names)
            if not valid:
                raise ServiceError('knowledge_vector_schema_mismatch',409,'知识向量集合结构不符合本章约定，请人工核查。')
            self.client.load_collection('knowledge')

    @staticmethod
    def _id(value):
        if type(value) is not int or not 1<=value<=2**63-1:raise ValueError('chunk id outside signed INT64 range')

    @staticmethod
    def _vector(vector):
        if len(vector)!=1024 or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector) or not any(vector):
            raise ValueError('invalid dense vector')

    def upsert(self,items: list[VectorItem]) -> list[int]:
        for item in items:
            self._id(item.id);self._vector(item.vector)
            if len(item.fingerprint)!=64:raise ValueError('invalid fingerprint')
        if not items:return []
        with self._lock:
            try:
                result=self.client.upsert('knowledge',[{'id':item.id,'embedding':item.vector,'fingerprint':item.fingerprint} for item in items])
                ids=result['ids']
                if set(ids)!={item.id for item in items} or result['upsert_count']!=len(items):raise store_error()
                return ids
            except ServiceError:raise
            except Exception:raise store_error() from None

    def search(self,vector: list[float],limit: int,*,timeout_seconds: float | None=None) -> list[SearchHit]:
        self._vector(vector)
        if not 1<=limit<=100:raise ValueError('invalid search limit')
        end=time.monotonic()+(timeout_seconds if timeout_seconds is not None else 10)
        if not self._lock.acquire(timeout=max(0,end-time.monotonic())):raise store_error()
        try:
            remaining=end-time.monotonic()
            if remaining<=0:raise store_error()
            rows=self.client.search('knowledge',data=[vector],anns_field='embedding',limit=limit,
                output_fields=['fingerprint'],search_params={'metric_type':'COSINE','params':{}},timeout=remaining)[0]
            return [SearchHit(int(row['id']),float(row['distance']),row['entity']['fingerprint']) for row in rows]
        except ServiceError:raise
        except Exception:raise store_error() from None
        finally:self._lock.release()

    def fingerprints(self,ids: list[int]) -> dict[int,str]:
        if not ids:return {}
        for value in ids:self._id(value)
        with self._lock:
            try:
                rows=self.client.get('knowledge',ids=ids,output_fields=['id','fingerprint'])
                return {row['id']:row['fingerprint'] for row in rows}
            except Exception:raise store_error() from None

    def close(self) -> None:
        with self._lock:
            if self.client is not None:
                self.client.close();self.client=None
