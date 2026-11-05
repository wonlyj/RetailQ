"""CLI for frozen-corpus four-strategy evaluations."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import sys

from retailq.knowledge.corpus import _write
from retailq.knowledge.evaluation.report import build_report
from retailq.knowledge.evaluation.store import EvaluationStore

ROOT=Path(__file__).resolve().parents[1]
EVAL_ROOT=(ROOT/'artifacts/evals/ch04').resolve()


def build_identity(settings,snapshot,split,*,files=None,server_version=None,policy_hash=None):
    from retailq.knowledge.evaluation.contracts import read_cases
    from retailq.knowledge.policy import policy_identity
    if split not in ('calibration','test'): raise ValueError('invalid split')
    if files is None:
        files=sorted((ROOT/'retailq').rglob('*.py'))+sorted((ROOT/'retailq/prompts').glob('*.txt'))+[
            ROOT/'scripts/evaluate_rag.py',ROOT/'scripts/calibrate_rag.py',ROOT/'evals/ch04_cases.jsonl',ROOT/'examples/knowledge/synonyms.json',
            ROOT/'requirements.lock']
    digest=sha256()
    for path in files:
        path=Path(path)
        digest.update(path.name.encode('utf-8'));digest.update(b'\0');digest.update(path.read_bytes());digest.update(b'\0')
    policy=Path(settings.rag_confidence_path)
    if not policy.is_absolute(): policy=ROOT/policy
    if policy_hash is None:
        policy_hash=sha256(policy.read_bytes()).hexdigest() if split=='test' and policy.is_file() else None
    return {'split':split,'expected':80 if split=='calibration' else 320,
            'case_ids':[c.eval_id for c in read_cases(ROOT/'evals/ch04_cases.jsonl') if c.split==split],
            'policy_identity':policy_identity(settings,snapshot,'hybrid_rerank'),
            'corpus_hash':snapshot.corpus_hash,'collection':snapshot.collection,'source_hash':digest.hexdigest(),
            'policy_hash':policy_hash,'python':sys.version.split()[0],
            'milvus_server':server_version,
            'services':{'milvus_uri':settings.milvus_uri,'mysql_host':settings.mysql_host,
                        'mysql_port':settings.mysql_port,'mysql_database':settings.mysql_database},
            'dependencies':{name:version(name) for name in ('pymilvus','sqlalchemy','langchain-openai','httpx')},
            'models':{'embedding':settings.embedding_model,'rerank':settings.rerank_model,'chat':settings.openai_model,
                      'embedding_url':settings.embedding_base_url,'rerank_url':settings.rerank_base_url,
                      'chat_url':settings.openai_base_url,'structured_method':settings.structured_output_method},
            'input_budget':settings.input_budget,'mode':'provisional' if split=='calibration' else 'calibrated'}


def safe_output(path):
    path=Path(path)
    if not path.is_absolute(): path=ROOT/path
    result=path.resolve()
    if result==EVAL_ROOT or not result.is_relative_to(EVAL_ROOT):
        raise ValueError('evaluation output must be a new directory below artifacts/evals/ch04')
    return result


def report_command(store, *, refresh_sealed=False):
    manifest=store.manifest()
    identity=manifest['identity']
    report=build_report(store.rows(),split=identity['split'],expected=identity['expected'])
    report['rows_complete']=report['complete']
    report['complete']=report['rows_complete'] and manifest['status']=='completed'
    report['identity']=identity
    report['status']=manifest['status']
    report['failure_count']=len(store.errors())
    report['failures_by_phase']=dict(Counter(e['phase'] for e in store.errors()))
    if manifest['status']=='sealed' and not refresh_sealed:
        return report
    _write(store.root/'report.json',report)
    lines=['# 第四章评估报告','',f"分集：{report['split']}；完成：{report['complete']}；"
           f"记录：{report['observed']}/{report['expected']}。",'',
           '| 策略 | 已完成 | Recall@10 | MRR@10 | Faithfulness 宏平均 | 作答 | 拒答 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    def fmt(v): return 'N/A' if v is None else f'{v:.4f}'
    for strategy,summary in report['overall'].items():
        lines.append(f"| {strategy} | {summary['completed']} | {fmt(summary['recall'].get('10'))} | "
                     f"{fmt(summary['mrr'])} | {fmt(summary['faithfulness_macro'])} | {summary['answered']} | {summary['refused']} |")
    lines+=['','## 拒答与覆盖','','| 策略 | 可答题拒答 / 完成数 | 不可答题拒答 / 完成数 | 引用回答 / 作答数 | 已裁判 / 作答数 |',
            '|---|---:|---:|---:|---:|']
    def ratio(n,d): return f'{n} / {d}' if d else 'N/A (0 / 0)'
    for strategy,summary in report['overall'].items():
        lines.append(f"| {strategy} | {ratio(summary['answerable_refusals'],summary['answerable_completed'])} | "
                     f"{ratio(summary['unanswerable_refusals'],summary['unanswerable_completed'])} | "
                     f"{ratio(summary['cited_answers'],summary['answered'])} | {ratio(summary['judge_answered'],summary['answered'])} |")
    lines+=['','## 按问题类型','',
            '| 类型 | 策略 | 已完成 | Recall@10 | MRR@10 | Faithfulness 宏平均 | 作答 | 拒答 |',
            '|---|---|---:|---:|---:|---:|---:|---:|']
    for bucket,strategies in report['by_bucket'].items():
        for strategy,summary in strategies.items():
            lines.append(f"| {bucket} | {strategy} | {summary['completed']} | {fmt(summary['recall'].get('10'))} | "
                         f"{fmt(summary['mrr'])} | {fmt(summary['faithfulness_macro'])} | {summary['answered']} | {summary['refused']} |")
    lines+=['','按难度的完整指标、分母、失败记录和身份见 report.json。',
            '无可用分母时为 N/A；同模型裁判可能存在偏差。']
    (store.root/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def seal_run(store,faith,run_id,reason):
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('seal reason is required')
    state=store.manifest()
    if state['status']=='running':
        report_command(store)
        store.begin_sealing(reason)
    elif state['status']!='sealing' or state.get('reason')!=reason:
        raise ValueError('evaluation is not sealing with this reason')
    try:
        terminal=getattr(faith,'root',Path('.'))/(run_id+'.json')
        if terminal.exists():
            faith._acquire()
            receipt=json.loads(terminal.read_text(encoding='utf-8'))
            if (receipt.get('run_id')!=run_id or receipt.get('status')!='sealed' or
                    receipt.get('identity')!=store.manifest()['identity'] or receipt.get('reason')!=reason):
                raise ValueError('seal receipt mismatch')
            faith._recover_terminal()
        else:
            faith.resume(run_id)
            faith.seal(run_id,reason)
        store.finish_sealing()
        report_command(store,refresh_sealed=True)
    finally:
        faith.close()


async def execute_real(output, split, *, resume=False):
    from retailq.config import Settings
    from retailq.db import Database
    from retailq.knowledge.answer import prepare_bundle
    from retailq.knowledge.evaluation.contracts import load_cases
    from retailq.knowledge.evaluation.judge import FaithfulnessJudge
    from retailq.knowledge.evaluation.runner import EvaluationRunner, _owned_thread
    from retailq.knowledge.faith_repository import FaithEvaluationCase, FaithRepository
    from retailq.knowledge.faith_store import FaithStore
    from retailq.knowledge.policy import freeze_evaluation_policy
    from retailq.knowledge.query import QueryNormalizer
    from retailq.knowledge.rag_gateway import OpenAIRAGGateway
    from retailq.knowledge.rerank import Reranker
    from retailq.knowledge.retrieval import RetrievalEngine
    from retailq.knowledge.runtime import KnowledgeRuntime

    settings=Settings().model_copy(update={'knowledge_enabled':True})
    if not settings.is_ready or not settings.is_knowledge_configured or not settings.effective_rerank_key:
        raise ValueError('configured chat, embedding, reranking and database are required')
    if len(settings.openai_model)>64: raise ValueError('judge model exceeds database limit')
    database=Database(settings)
    runtime=reranker=gateway=judge=faith=None
    store=None
    try:
        runtime=KnowledgeRuntime.open(settings,database)
        snapshot=runtime.corpus.active()
        cases=load_cases(ROOT/'evals/ch04_cases.jsonl',snapshot,repository=runtime.service.repository)
        with runtime.vectors.open(snapshot.collection) as vector_probe:
            server_version=vector_probe.client.get_server_version()
        frozen_policy,policy_hash=freeze_evaluation_policy(settings,snapshot) if split=='test' else (None,None)
        identity=build_identity(settings,snapshot,split,server_version=server_version,policy_hash=policy_hash)
        if split=='test' and identity['policy_hash'] is None:
            raise ValueError('test evaluation requires a calibrated policy')
        store=EvaluationStore.resume(output,identity) if resume else EvaluationStore.open_new(output,identity)
        faith=FaithStore(runtime.root/'faith',runtime.coordinator)
        repository=FaithRepository(database,faith)
        run_id=output.name
        terminal=faith.root/(run_id+'.json')
        if resume and store.manifest()['status']=='finalizing' and terminal.exists():
            faith._acquire()
            try:
                receipt=json.loads(terminal.read_text(encoding='utf-8'))
                if (receipt.get('run_id')!=run_id or receipt.get('identity')!=identity or receipt.get('status')!='completed'
                        or not isinstance(receipt.get('expected'),list) or not repository.verify_receipts(run_id,receipt['expected'])):
                    raise ValueError('terminal faith receipts do not match')
                faith._recover_terminal()
                store.finish_finalization()
                return report_command(store)
            finally:
                faith.close()
        if resume: faith.resume(run_id)
        else: faith.begin(run_id,identity)
        if store.manifest()['status']=='finalizing':
            faith.complete(run_id)
            store.finish_finalization()
            return report_command(store)
        synonyms=json.loads((ROOT/'examples/knowledge/synonyms.json').read_text(encoding='utf-8'))['terms']
        normalizer=QueryNormalizer(runtime.query_gateway.normalize,synonyms)
        reranker=Reranker(settings.rerank_base_url,settings.effective_rerank_key,
                          proxy=settings.rerank_proxy.get_secret_value(),timeout_seconds=settings.rerank_timeout_seconds)
        gateway=OpenAIRAGGateway(settings)
        judge=FaithfulnessJudge(settings)
        prompt=(ROOT/'retailq/prompts/rag_answer.txt').read_text(encoding='utf-8')
        async def ledger(case,decision,bundle,judged):
            reason='；'.join(c['text']+': '+c['reason'] for c in judged.claims if not c['supported'])
            receipt=FaithEvaluationCase(case.eval_id,case.bucket,case.query,'hybrid_rerank',decision.answer,
                                        reason,tuple(asdict(c) for c in bundle.citations),settings.openai_model)
            await _owned_thread(repository.record,receipt,eval_run_id=run_id)
        with runtime.vectors.open(snapshot.collection) as vector_store:
            engine=RetrievalEngine(runtime.service.repository,runtime.embedding,vector_store,reranker)
            runner=EvaluationRunner(snapshot=snapshot,normalize=normalizer.understand,retrieve=engine.retrieve,
                gateway=gateway,judge=judge,bundle_factory=lambda result:prepare_bundle(result,system_prompt=prompt,input_budget=settings.input_budget),
                store=store,timeout_seconds=settings.knowledge_query_timeout_seconds,
                policy=frozen_policy,ledger=ledger)
            await runner.run(cases,split=split)
        report=report_command(store)
        if not report['rows_complete']: raise ValueError('evaluation is incomplete')
        store.begin_finalization()
        faith.complete(run_id)
        store.finish_finalization()
        report=report_command(store)
        return report
    finally:
        if store is not None:
            try: report_command(store)
            except (OSError,ValueError,KeyError,TypeError): pass
        if faith is not None: faith.close()
        if judge is not None: await judge.aclose()
        if gateway is not None: await gateway.aclose()
        if reranker is not None: reranker.close()
        if runtime is not None: runtime.close()
        database.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='第四章冻结语料评估')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--split',choices=('calibration','test'),required=True)
    run.add_argument('--mode',choices=('real',),required=True);run.add_argument('--output',required=True)
    for name in ('resume','report','seal'):
        item=sub.add_parser(name);item.add_argument('--output',required=True)
        if name=='seal':item.add_argument('--reason',required=True)
    args=parser.parse_args(argv)
    try:
        output=safe_output(args.output)
        if args.command=='run':
            report=asyncio.run(execute_real(output,args.split))
        elif args.command=='resume':
            previous=EvaluationStore(output).manifest()
            report=asyncio.run(execute_real(output,previous['identity']['split'],resume=True))
        elif args.command=='report':
            report=report_command(EvaluationStore(output))
        else:
            from retailq.config import Settings
            from retailq.coordination import Coordinator
            from retailq.knowledge.faith_store import FaithStore
            settings=Settings()
            coordinator=Coordinator.for_settings(settings)
            root=Path(settings.knowledge_data_dir)
            if not root.is_absolute(): root=ROOT/root
            store=EvaluationStore(output)
            faith=FaithStore(root/'faith',coordinator)
            seal_run(store,faith,output.name,args.reason)
            report=report_command(store)
        print(json.dumps({'complete':report['complete'],'report':str(output/'report.json')},ensure_ascii=False))
        return 0 if report['complete'] else 1
    except (ValueError,OSError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({'error':'evaluation failed; inspect checkpoint and error receipt'},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
