"""Reviewable candidate collection for relevance calibration; no automatic labels."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from retailq.knowledge.corpus import _write

ROOT=Path(__file__).resolve().parents[1]


def read_relevance_cases(path):
    rows=[json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    keys={'id','split','family','kind','query','expected','source','answerable'}
    ids=set(); families={}; queries=set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)!=keys or type(row['answerable']) is not bool or
                any(not isinstance(row[k],str) or not row[k].strip() for k in keys-{'answerable'}) or
                row['split'] not in ('calibration','test','regression') or row['id'] in ids or row['query'] in queries):
            raise ValueError('invalid or duplicate relevance case')
        if row['family'] in families and families[row['family']]!=row['split']:
            raise ValueError('question family crosses splits')
        ids.add(row['id']); queries.add(row['query']); families[row['family']]=row['split']
    if not rows: raise ValueError('empty relevance cases')
    return rows


def validate_case_sources(cases, source_root):
    from retailq.knowledge.text import normalize_text
    source_root=Path(source_root).resolve(); digests={}
    for case in cases:
        if case['source']=='无':
            if case['answerable']: raise ValueError('answerable case has no source')
            continue
        for ref in case['source'].split(' + '):
            parts=ref.split(' / ')
            if len(parts)<2: raise ValueError('source section missing')
            path=(source_root/parts[0]).resolve()
            if path.parent!=source_root or not path.is_file(): raise ValueError('invalid source path')
            raw=path.read_bytes(); content=raw.decode('utf-8-sig'); stack=[]; heading_paths=[]
            for line in content.splitlines():
                match=re.match(r'^(#{1,6})\s+(.+?)\s*$',line)
                if not match: continue
                level=len(match.group(1))
                while stack and stack[-1][0]>=level: stack.pop()
                stack.append((level,match.group(2)))
                heading_paths.append([title for _,title in stack])
            def matches(titles):
                requested=iter(parts[1:])
                current=next(requested,None)
                for title in titles:
                    if current is not None and current in title: current=next(requested,None)
                return current is None
            if not any(matches(titles) for titles in heading_paths): raise ValueError('source section missing')
            digests[parts[0]]=sha256(normalize_text(content).encode('utf-8')).hexdigest()
    return digests


def validate_isolation(settings):
    root=Path(settings.knowledge_data_dir)
    if not root.is_absolute(): root=ROOT/root
    if (settings.mysql_host not in ('localhost','127.0.0.1') or settings.mysql_port!=3308 or
            settings.mysql_database!='retailq_test' or settings.milvus_uri!='http://127.0.0.1:19531' or
            settings.milvus_collection_prefix!='eval_ch04' or
            root.resolve()!=(ROOT/'artifacts/ch04-eval-knowledge').resolve()):
        raise ValueError('candidate collection requires isolated 3308/19531 services and data directory')


def collect_calibration(output):
    from retailq.config import Settings
    from retailq.db import Database
    from retailq.knowledge.policy import policy_identity
    from retailq.knowledge.query import QueryNormalizer
    from retailq.knowledge.rerank import Reranker
    from retailq.knowledge.retrieval import RetrievalEngine
    from retailq.knowledge.retrieval_contracts import RetrievalFilters
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.evaluation.runner import STRATEGIES

    output=Path(output).resolve(); allowed=(ROOT/'artifacts/evals/ch04').resolve()
    if output==allowed or not output.is_relative_to(allowed) or output.exists():
        raise ValueError('output must be a new chapter-four evaluation directory')
    cases_path=ROOT/'evals/ch04_relevance_cases.jsonl'
    cases=read_relevance_cases(cases_path)
    source_digests=validate_case_sources(cases,ROOT/'examples/knowledge')
    settings=Settings().model_copy(update={'knowledge_enabled':True})
    validate_isolation(settings)
    db=Database(settings); runtime=reranker=None
    try:
        runtime=KnowledgeRuntime.open(settings,db); snapshot=runtime.corpus.active()
        active_sources={source.key:source.digest for source in snapshot.sources}
        if any(active_sources.get(key)!=digest for key,digest in source_digests.items()):
            raise ValueError('case sources differ from active frozen corpus')
        identity={'policy_identity':policy_identity(settings,snapshot,'all',version=2),
                  'case_hash':sha256(cases_path.read_bytes()).hexdigest(),
                  'case_ids':[case['id'] for case in cases if case['split']=='calibration'],
                  'source_digests':source_digests,
                  'collection':snapshot.collection,'corpus_hash':snapshot.corpus_hash,
                  'script_hash':sha256(Path(__file__).read_bytes()).hexdigest()}
        output.mkdir(parents=True)
        report={'complete':False,'split':'calibration','identity':identity,'rows':[],'understanding':{},'errors':[]}
        _write(output/'checkpoint.json',report)
        synonyms=json.loads((ROOT/'examples/knowledge/synonyms.json').read_text(encoding='utf-8'))['terms']
        normalizer=QueryNormalizer(runtime.query_gateway.normalize,synonyms)
        reranker=Reranker(settings.rerank_base_url,settings.effective_rerank_key,
                          proxy=settings.rerank_proxy.get_secret_value(),timeout_seconds=settings.rerank_timeout_seconds)
        with runtime.vectors.open(snapshot.collection) as vectors:
            engine=RetrievalEngine(runtime.service.repository,runtime.embedding,vectors,reranker)
            for case in (c for c in cases if c['split']=='calibration'):
                try:
                    understood=normalizer.understand(case['query'],deadline=time.monotonic()+90)
                    report['understanding'][case['id']]=asdict(understood)
                    for strategy in STRATEGIES:
                        result=engine.retrieve(understood,snapshot,strategy,RetrievalFilters(),deadline=time.monotonic()+90)
                        report['rows'].append({'eval_id':case['id'],'strategy':strategy,'phase':'complete',
                            'answerable':case['answerable'],'candidates':[asdict(candidate) for candidate in result.candidates]})
                        _write(output/'checkpoint.json',report)
                    print(case['id'],flush=True)
                except Exception as exc:
                    report['errors'].append({'id':case['id'],'code':getattr(exc,'code',type(exc).__name__)})
                    _write(output/'checkpoint.json',report)
                    raise
        report['complete']=True
        _write(output/'checkpoint.json',report)
    finally:
        if reranker: reranker.close()
        if runtime: runtime.close()
        db.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    collect_calibration(args.output)


if __name__=='__main__': main()
