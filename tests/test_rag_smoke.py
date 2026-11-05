import pytest

from scripts.smoke_rag import parse_sse


def test_model_case_requires_a_cited_answer_not_a_refusal():
    from scripts.smoke_rag import verify_model_case
    with pytest.raises(ValueError):verify_model_case({'answer':'知识库没有足够可靠的依据','conversation_id':1})
    verify_model_case({'answer':'适用 1.5kg 至 8kg [1]','source_document':{'section_path':'MH-LP100'}})


def test_tool_receipt_only_contains_the_final_evidence_bundle():
    from dataclasses import dataclass
    from types import SimpleNamespace
    from retailq.services.chat import rag_tool_receipt
    @dataclass
    class Citation:
        chunk_id:int
    candidates={'ok':True,'data':{'found':True,'items':[{'id':1,'answer':'included'},{'id':2,'answer':'excluded'}]}}
    bundle=SimpleNamespace(bundle_hash='hash',citations=(Citation(chunk_id=1),))
    receipt=rag_tool_receipt(candidates,bundle)
    assert receipt['data']['items']==[{'id':1,'answer':'included'}]
    refused=rag_tool_receipt(candidates,None)
    assert refused['data']=={'found':False,'items':[]}


@pytest.mark.anyio
async def test_refusal_persists_no_rejected_candidate_in_tool_history():
    import json
    from types import SimpleNamespace
    from retailq.config import Settings
    from retailq.services.chat import ChatService
    captured=[]
    class Repository:
        def append_message(self,*args):captured.append(json.loads(args[3]))
    class Finalizer:
        def finalize(self,*args,**kwargs):return 1
    class Policy:
        def allows(self,result):return False
    service=ChatService(Repository(),object(),Settings(_env_file=None),rag_gateway=object(),rag_repository=Finalizer(),confidence_policy=Policy())
    turn=SimpleNamespace(conversation_id=1,user_id='user',registry=SimpleNamespace(context=SimpleNamespace(retrieval_result=object())))
    outcome=SimpleNamespace(payload={'ok':True,'data':{'found':True,'items':[{'id':1,'answer':'rejected evidence'}]}})
    events=[event async for event in service._rag_answer(turn,{'id':'call1','name':'query_faq'},outcome)]
    assert events[-1].event=='done'
    assert captured[0]['data']=={'found':False,'items':[]}


def test_sse_parser_handles_multiline_data_and_requires_terminal_event():
    lines=['event: meta','data: {"conversation_id": 1}','','event: token','data: {"text":"答案"}','','event: done','data: {"finish_reason":"stop"}','']
    events=parse_sse(lines)
    assert events[-1]==('done',{'finish_reason':'stop'})
    with pytest.raises(ValueError): parse_sse(lines[:-3])


def test_sse_parser_rejects_events_after_terminal():
    with pytest.raises(ValueError):
        parse_sse(['event: done','data: {"finish_reason":"stop"}','','event: token','data: {"text":"late"}',''])


def test_smoke_scope_rejects_demo_server_and_database(tmp_path):
    from types import SimpleNamespace
    from pathlib import Path
    from scripts.smoke_rag import validate_scope
    settings=SimpleNamespace(mysql_host='127.0.0.1',mysql_port=3308,mysql_database='retailq_test')
    with pytest.raises(ValueError): validate_scope('http://127.0.0.1:8000',Path('artifacts/verification/ch04/scope-guard-test'),settings)
    base,_=validate_scope('http://127.0.0.1:18801',Path('artifacts/verification/ch04/scope-guard-test'),settings)
    assert base=='http://127.0.0.1:18801'


def test_citation_must_map_to_frozen_document_section():
    from scripts.smoke_rag import verify_citation
    cite={'source_key':'a.md','source_version':'hash','section_path':'a/b','text':'事实一\n事实二'}
    doc={'source_key':'a.md','source_version':'hash','section_path':'a/b','text':'# 标题\n事实一\n事实二','anchor':{'start':2,'end':3}}
    verify_citation(cite,doc)
    doc['text']='# 标题\n事实一\n别的内容'
    with pytest.raises(ValueError):verify_citation(cite,doc)
