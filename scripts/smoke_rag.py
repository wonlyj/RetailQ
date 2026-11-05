"""Read and validate actual chat event streams for chapter-four acceptance."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


def parse_sse(lines):
    events=[];event='';data=[];terminal=False
    for line in lines:
        if line.startswith(':'): continue
        if line.startswith('event:'): event=line[6:].strip()
        elif line.startswith('data:'): data.append(line[5:].removeprefix(' '))
        elif not line and data:
            if terminal: raise ValueError('event after terminal')
            payload=json.loads('\n'.join(data))
            events.append((event,payload))
            terminal=event in ('done','error')
            event='';data=[]
    if data or not terminal: raise ValueError('incomplete event stream')
    return events


def validate_scope(base_url, output, settings):
    root=(Path(__file__).resolve().parents[1]/'artifacts/verification/ch04').resolve()
    output=Path(output).resolve();url=urlsplit(base_url)
    if (url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.port not in (18800,18801) or url.username or url.password
            or url.path not in ('','/') or url.query or url.fragment or output==root or not output.is_relative_to(root)
            or output.exists() or settings.mysql_host not in ('127.0.0.1','localhost')
            or settings.mysql_port!=3308 or settings.mysql_database!='retailq_test'):
        raise ValueError('smoke only permits a new ch04 artifact directory and the isolated 3308 database')
    return base_url.rstrip('/'),output


def verify_citation(citation,document):
    if any(document.get(key)!=citation.get(key) for key in ('source_key','source_version','section_path')):
        raise ValueError('citation source identity mismatch')
    if citation.get('source_key')=='review':
        if document.get('text')!=citation.get('text'):raise ValueError('review source text mismatch')
        return
    lines=document['text'].splitlines();anchor=document.get('anchor') or {}
    start,end=anchor.get('start'),anchor.get('end')
    if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):
        raise ValueError('citation anchor invalid')
    section='\n'.join(lines[start-1:end])
    if any(line.strip() not in section for line in citation['text'].splitlines() if line.strip()):
        raise ValueError('citation quote absent from document section')


def verify_model_case(row):
    answer=row.get('answer','')
    source=row.get('source_document') or {}
    if ('1.5' not in answer or '8' not in answer or not re.search(r'\[\d+\]',answer)
            or 'MH-LP100' not in source.get('section_path','')):
        raise ValueError('model-specific question did not produce the cited expected answer')


def chat(client,base_url,question,user_id,conversation_id=None):
    body={'message':question,'history':[],'user_id':user_id}
    if conversation_id is not None: body['conversation_id']=conversation_id
    with client.stream('POST',base_url+'/api/chat/stream',json=body) as response:
        response.raise_for_status()
        if 'text/event-stream' not in response.headers.get('content-type',''): raise ValueError('not an event stream')
        events=parse_sse(response.iter_lines())
    return events


def main(argv=None):
    from sqlalchemy import text
    from retailq.config import Settings
    from retailq.db import Database
    parser=argparse.ArgumentParser(description='真实客服RAG隔离验收')
    parser.add_argument('--base-url',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    settings=Settings();base,output=validate_scope(args.base_url,args.output,settings)
    output.mkdir(parents=True)
    database=Database(settings);user_id='ch04-smoke-'+uuid4().hex
    report={'user_id':user_id,'cases':[],'automated_checks_passed':False,'semantic_review':'required'}
    try:
        with httpx.Client(timeout=120,trust_env=False) as client:
            for question in ('电子发票多久发送到邮箱？','MH-LP100适用猫咪体重是多少？','会员积分有效期到底多久？'):
                events=chat(client,base,question,user_id)
                row={'question':question,'events':events}
                report['cases'].append(row)
                (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                if events[-1][0]!='done' or events[-1][1].get('finish_reason')!='stop': raise ValueError('chat did not finish')
                meta=next((data for kind,data in events if kind=='meta'),None)
                if not meta or not any(kind=='tool_status' and data.get('name')=='query_faq' for kind,data in events):
                    raise ValueError('actual FAQ routing missing')
                row['conversation_id']=meta['conversation_id']
                row['answer']=''.join(data.get('text','') for kind,data in events if kind=='token')
                source=next((data for kind,data in events if kind=='sources'),None)
                if source:
                    cited={int(number) for number in re.findall(r'\[(\d+)\]',row['answer'])}
                    citation=next((item for item in source['citations'] if item['number'] in cited),None)
                    if citation is None: raise ValueError('answer does not cite supplied source')
                    url=f"{base}/api/conversations/{meta['conversation_id']}/tool-calls/{quote(source['tool_call_id'],safe='')}/sources/{citation['number']}/document"
                    response=client.get(url,params={'user_id':user_id});response.raise_for_status()
                    row['source_document']=response.json()
                    verify_citation(citation,row['source_document'])
            first=report['cases'][0]
            if not first.get('source_document') or '[' not in first['answer']:
                raise ValueError('model answer has no cited document')
            missing=report['cases'][-1]
            with database.engine.connect() as connection:
                entries=connection.execute(text('SELECT raw_question, source, reason, created_at FROM low_confidence_questions WHERE conversation_id=:cid'),{'cid':missing['conversation_id']}).mappings().all()
            missing['pool']=[dict(entry) | {'created_at':str(entry['created_at'])} for entry in entries]
            if not any(row['raw_question']==missing['question'] and row['source'] in ('retrieval_low_conf','self_check') and row['reason'] for row in missing['pool']):
                raise ValueError('missing refusal pool receipt')
            verify_model_case(report['cases'][1])
        report['automated_checks_passed']=True
        return 0
    finally:
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        database.close()


if __name__=='__main__':
    raise SystemExit(main())
