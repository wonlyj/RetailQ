"""Deterministic vectors test storage/protocol only, never semantic quality."""
import math

import pytest


class TestBudget:
    def validate(self,text):
        if not text.strip():raise ValueError('empty')


class FakeEmbedding:
    def embed(self,texts,*,deadline=None):
        result=[]
        for text in texts:
            vector=[0.0]*1024
            if text=='比较':vector[0],vector[1]=0.5,math.sqrt(0.75)
            elif any(word in text for word in ['运费','配送查询']):vector[1]=1.0
            elif '发票' in text:vector[2]=1.0
            else:vector[0]=1.0
            result.append(vector)
        return result
    def close(self):pass


@pytest.fixture
def service(test_database,tmp_path):
    from retailq.knowledge.service import KnowledgeService
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.vectors import MilvusKnowledgeStore
    from tests.test_knowledge_vectors import identity
    with MilvusKnowledgeStore(tmp_path/'knowledge.db',identity()) as vectors:
        instance=KnowledgeService(KnowledgeRepository(test_database),FakeEmbedding(),vectors,TestBudget(),min_score=0.2)
        instance.import_faq();instance.vectorize_pending()
        yield instance
