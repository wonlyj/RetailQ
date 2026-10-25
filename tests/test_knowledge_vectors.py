from pathlib import Path
import subprocess
import sys

import pytest

from retailq.errors import ServiceError

pytestmark=pytest.mark.lite


def identity():
    return {'provider':'siliconflow','model':'BAAI/bge-m3','dimension':'1024','metric':'COSINE','text_template_version':'1','normalization_version':'l2-v1','tokenizer_revision':'5617a9f61b028005a4858fdac845db406aefb181','schema_version':'1'}


def store(path,metadata=None):
    from retailq.knowledge.vectors import MilvusKnowledgeStore
    return MilvusKnowledgeStore(path,metadata or identity())


def item(number,axis=0):
    from retailq.knowledge.contracts import VectorItem
    vector=[0.0]*1024;vector[axis]=1.0
    return VectorItem(number,vector,'a'*64)


def test_lite_explicit_schema_and_cosine_order(tmp_path):
    with store(tmp_path/'knowledge.db') as db:
        assert set(db.upsert([item(1),item(2,1)]))=={1,2}
        hits=db.search(item(1).vector,2)
        assert hits[0].id==1 and hits[0].score>hits[1].score
        assert hits[0].fingerprint=='a'*64
        assert db.fingerprints([1,3])=={1:'a'*64}


def test_upsert_same_primary_key_survives_reopen(tmp_path):
    path=tmp_path/'knowledge.db'
    with store(path) as db:db.upsert([item(1)])
    with store(path) as db:
        db.upsert([item(1,1)])
        hits=db.search(item(1,1).vector,10)
        assert [h.id for h in hits]==[1]
        assert hits[0].score>0.99


def test_schema_or_identity_mismatch_is_rejected(tmp_path):
    path=tmp_path/'knowledge.db'
    with store(path) as db:db.upsert([item(1)])
    changed=identity()|{'model':'different-model'}
    with pytest.raises(ServiceError):store(path,changed)
    from pymilvus import MilvusClient
    bad=tmp_path/'bad.db'
    client=MilvusClient(str(bad));client.create_collection('knowledge',dimension=4);client.close()
    with pytest.raises(ServiceError):store(bad)


@pytest.mark.parametrize('bad_id',[0,-1,2**63,True])
def test_signed_int64_range_is_enforced(tmp_path,bad_id):
    with store(tmp_path/'knowledge.db') as db:
        with pytest.raises(ValueError):db.upsert([item(bad_id)])


def test_second_process_cannot_own_directory(tmp_path):
    from retailq.knowledge.ownership import DirectoryOwner
    code='from pathlib import Path; from retailq.knowledge.ownership import DirectoryOwner; import sys; DirectoryOwner(Path(sys.argv[1])).__enter__()'
    with DirectoryOwner(tmp_path):
        child=subprocess.run([sys.executable,'-X','utf8','-c',code,str(tmp_path)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        assert child.returncode!=0
        with pytest.raises(ServiceError):
            with DirectoryOwner(tmp_path):pass
    with DirectoryOwner(tmp_path):pass


def test_owner_recovers_after_process_termination(tmp_path):
    from retailq.knowledge.ownership import DirectoryOwner
    code='from pathlib import Path; from retailq.knowledge.ownership import DirectoryOwner; import sys; owner=DirectoryOwner(Path(sys.argv[1])); owner.__enter__(); print("ready",flush=True); sys.stdin.readline()'
    child=subprocess.Popen([sys.executable,'-X','utf8','-c',code,str(tmp_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    try:
        assert child.stdout.readline().strip()=='ready'
        with pytest.raises(ServiceError):
            with DirectoryOwner(tmp_path):pass
    finally:
        child.kill();child.communicate(timeout=10)
    with DirectoryOwner(tmp_path):pass
