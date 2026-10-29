"""Native Milvus full-text, dense and server-side RRF retrieval."""
from hashlib import sha256
import json
import math
import re

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from retailq.errors import ServiceError
from retailq.knowledge.retrieval_contracts import RawHit, RetrievalFilters


def _error(message='知识检索服务不可用，请稍后重试。'):
    return ServiceError('knowledge_vector_unavailable', 503, message)


class MilvusKnowledgeStore:
    def __init__(self, uri: str, identity: dict, *, collection: str):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,254}', collection):
            raise ValueError('invalid collection name')
        self.uri, self.collection = uri, collection
        self.identity = sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.client = MilvusClient(uri)

    def close(self):
        self.client.close()

    def exists(self):
        return self.client.has_collection(self.collection)

    def count(self):
        rows = self.client.query(self.collection, filter='id >= 0', output_fields=['count(*)'], consistency_level='Strong')
        if len(rows) != 1 or type(rows[0].get('count(*)')) is not int:
            raise _error()
        return rows[0]['count(*)']

    def ensure_collection(self):
        if not self.client.has_collection(self.collection):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False, description='retailq:' + self.identity)
            schema.add_field('id', DataType.INT64, is_primary=True)
            schema.add_field('dense', DataType.FLOAT_VECTOR, dim=1024)
            schema.add_field('text', DataType.VARCHAR, max_length=65535, enable_analyzer=True, analyzer_params={'type': 'chinese'})
            schema.add_field('sparse', DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field('fingerprint', DataType.VARCHAR, max_length=64)
            schema.add_field('category', DataType.VARCHAR, max_length=1024)
            schema.add_field('content_type', DataType.VARCHAR, max_length=128)
            schema.add_field('source_key', DataType.VARCHAR, max_length=1024)
            schema.add_field('source_version', DataType.VARCHAR, max_length=128)
            schema.add_function(Function(name='bm25', function_type=FunctionType.BM25, input_field_names=['text'], output_field_names=['sparse']))
            indexes = self.client.prepare_index_params()
            indexes.add_index(field_name='dense', index_type='FLAT', metric_type='COSINE')
            indexes.add_index(field_name='sparse', index_type='SPARSE_INVERTED_INDEX', metric_type='BM25')
            self.client.create_collection(self.collection, schema=schema, index_params=indexes, consistency_level='Strong')
        schema = self.client.describe_collection(self.collection)
        fields = {f['name']: f for f in schema['fields']}
        expected = {'id': DataType.INT64, 'dense': DataType.FLOAT_VECTOR, 'text': DataType.VARCHAR,
                    'sparse': DataType.SPARSE_FLOAT_VECTOR, 'fingerprint': DataType.VARCHAR,
                    'category': DataType.VARCHAR, 'content_type': DataType.VARCHAR,
                    'source_key': DataType.VARCHAR, 'source_version': DataType.VARCHAR}
        valid = (schema.get('description') == 'retailq:' + self.identity and set(fields) == set(expected)
                 and not schema.get('auto_id') and not schema.get('enable_dynamic_field'))
        if valid:
            valid = all(fields[name]['type'] == kind for name, kind in expected.items())
            valid &= bool(fields['id'].get('is_primary')) and fields['dense']['params'].get('dim') == 1024
            valid &= bool(fields['sparse'].get('is_function_output'))
            valid &= all(fields[name]['params'].get('max_length') == size for name, size in
                         (('text', 65535), ('fingerprint', 64), ('category', 1024), ('content_type', 128),
                          ('source_key', 1024), ('source_version', 128)))
            try:
                valid &= fields['text']['params'].get('enable_analyzer') == 'true'
                valid &= json.loads(fields['text']['params']['analyzer_params']) == {'type': 'chinese'}
            except (KeyError, TypeError, ValueError):
                valid = False
            functions = schema.get('functions', [])
            valid &= (len(functions) == 1 and functions[0].get('type') == FunctionType.BM25 and
                      functions[0].get('input_field_names') == ['text'] and functions[0].get('output_field_names') == ['sparse'])
            indexes = [self.client.describe_index(self.collection, name) for name in self.client.list_indexes(self.collection)]
            valid &= any(i.get('field_name') == 'dense' and i.get('metric_type') == 'COSINE' for i in indexes)
            valid &= any(i.get('field_name') == 'sparse' and i.get('metric_type') == 'BM25' and
                         i.get('index_type') == 'SPARSE_INVERTED_INDEX' for i in indexes)
        if not valid:
            raise ServiceError('knowledge_identity_mismatch', 409, '集合身份或结构不一致，请核查后重建。')
        self.client.load_collection(self.collection)

    @staticmethod
    def _vector(vector):
        if len(vector) != 1024 or any(type(x) not in (float, int) or not math.isfinite(x) for x in vector) or not any(vector):
            raise ValueError('invalid dense vector')

    @staticmethod
    def _filter(filters):
        filters = filters or RetrievalFilters()
        parts = []
        if filters.category is not None and filters.category_prefix is not None:
            raise ValueError('choose category or category_prefix')
        for field in ('category', 'content_type'):
            value = getattr(filters, field)
            if value is not None:
                if not isinstance(value, str) or not value or len(value.encode('utf-8')) > (1024 if field == 'category' else 128):
                    raise ValueError('invalid filter')
                parts.append(f'{field} == {json.dumps(value, ensure_ascii=False)}')
        if filters.category_prefix is not None:
            value = filters.category_prefix
            if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024:
                raise ValueError('invalid category prefix')
            pattern = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + ' / %'
            parts.append(f'(category == {json.dumps(value, ensure_ascii=False)} or category like {json.dumps(pattern, ensure_ascii=False)})')
        return ' and '.join(parts)

    def upsert(self, items):
        rows = []
        for item in items:
            if type(item.id) is not int or not 1 <= item.id < 2**63:
                raise ValueError('invalid id')
            self._vector(item.vector)
            if not re.fullmatch(r'[0-9a-f]{64}', item.fingerprint):
                raise ValueError('invalid fingerprint')
            for value, limit in ((item.text, 65535), (item.category, 1024), (item.content_type, 128),
                                 (item.source_key, 1024), (item.source_version, 128)):
                if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
                    raise ValueError('invalid text field')
            rows.append({'id': item.id, 'dense': item.vector, 'text': item.text, 'fingerprint': item.fingerprint,
                         'category': item.category, 'content_type': item.content_type,
                         'source_key': item.source_key, 'source_version': item.source_version})
        if not rows:
            return []
        if len({row['id'] for row in rows}) != len(rows):
            raise ValueError('duplicate ids')
        result = self.client.upsert(self.collection, rows)
        ids = result['ids']
        if result['upsert_count'] != len(rows) or len(ids) != len(rows) or set(ids) != {row['id'] for row in rows}:
            raise _error()
        return ids

    @staticmethod
    def _hits(result):
        return tuple(RawHit(int(row['id']), float(row['distance']), row['entity']['fingerprint']) for row in result[0])

    def dense_search(self, vector, limit, filters, *, timeout_seconds):
        self._vector(vector)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid limit')
        return self._hits(self.client.search(self.collection, [vector], anns_field='dense', filter=self._filter(filters), limit=limit,
                                             output_fields=['fingerprint'], search_params={'metric_type': 'COSINE'}, timeout=timeout_seconds))

    def bm25_search(self, text, limit, filters, *, timeout_seconds):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65535 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid query')
        return self._hits(self.client.search(self.collection, [text], anns_field='sparse', filter=self._filter(filters), limit=limit,
                                             output_fields=['fingerprint'], search_params={'metric_type': 'BM25'}, timeout=timeout_seconds))

    def hybrid_search(self, vector, text, filters, *, timeout_seconds):
        self._vector(vector)
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65535:
            raise ValueError('invalid query')
        expression = self._filter(filters)
        requests = [AnnSearchRequest([vector], 'dense', {'metric_type': 'COSINE'}, 50, expr=expression),
                    AnnSearchRequest([text], 'sparse', {'metric_type': 'BM25'}, 50, expr=expression)]
        return self._hits(self.client.hybrid_search(self.collection, requests, ranker=RRFRanker(k=60), limit=50,
                                                    output_fields=['fingerprint'], timeout=timeout_seconds))

    def fingerprints(self, ids):
        if any(type(i) is not int or not 1 <= i < 2**63 for i in ids):
            raise ValueError('invalid id')
        if not ids:
            return {}
        rows = self.client.query(self.collection, ids=ids, output_fields=['fingerprint'])
        return {int(row['id']): row['fingerprint'] for row in rows}
