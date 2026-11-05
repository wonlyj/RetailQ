"""Owned-process coordination primitives for isolated RAG recovery checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from retailq.coordination import Coordinator, RuntimeIdentity

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=(ROOT/'artifacts/verification/ch04').resolve()


def validate_output(output):
    path=Path(output).resolve()
    if path==EVIDENCE or not path.is_relative_to(EVIDENCE) or path.exists():
        raise ValueError('use a new directory under artifacts/verification/ch04')
    return path


def wait_for_owned_phase(marker, process, phase, *, timeout=90):
    marker=Path(marker)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise ValueError('owned process exited before checkpoint was confirmed')
        if marker.exists():
            value=json.loads(marker.read_text(encoding='utf-8'))
            if value.get('pid')!=process.pid:
                raise ValueError('checkpoint process identity mismatch')
            if value.get('phase')!=phase:
                raise ValueError('checkpoint phase mismatch')
            if process.poll() is not None:
                raise ValueError('owned process exited before checkpoint was confirmed')
            return value
        time.sleep(.05)
    raise TimeoutError('owned process did not reach checkpoint')


def run_lease_probe(output):
    output=validate_output(output);output.mkdir(parents=True)
    marker=output/'ready.json';root=output/'knowledge';locks=output/'locks'
    env=dict(os.environ);executable=sys._base_executable if os.name=='nt' else sys.executable
    if os.name=='nt':env['__PYVENV_LAUNCHER__']=sys.executable
    child=subprocess.Popen([executable,'-m','tests.support.ch04_worker','--root',str(root),'--locks',str(locks),'--marker',str(marker)],cwd=ROOT,env=env)
    report={'mode':'deterministic','scenario':'owned_process_maintenance_lease','passed':False}
    try:
        ready=wait_for_owned_phase(marker,child,'maintenance_held')
        identity=RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root)
        coordinator=Coordinator(locks,identity)
        probe=coordinator.try_acquire('maintenance')
        if probe is not None:
            probe.close()
            raise ValueError('lease was not exclusive')
        child.kill();child.wait(timeout=10)
        lease=coordinator.try_acquire('maintenance')
        if lease is None: raise ValueError('lease was not released after child exit')
        lease.close()
        report.update(passed=True,ready=ready,child_returncode=child.returncode)
        return report
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def run_real_checks(output,base_url='http://127.0.0.1:18800'):
    from retailq.config import Settings
    from scripts.smoke_rag import validate_scope
    output=validate_output(output)
    settings=Settings()
    validate_scope(base_url,output/'smoke',settings)
    if settings.milvus_uri.rstrip('/')!='http://127.0.0.1:19531':
        raise ValueError('real checks require the isolated 19531 Milvus')
    output.mkdir(parents=True)
    report={'mode':'real','passed':False,'checks':{},'limits':['worker PID routing and seal/leader handover require separate evidence',
             'smoke automated checks require human semantic review']}
    env=dict(os.environ);env['CH04_FAULT_DB']='1'
    # Keep Windows paths short: archived corpus files already contain two SHA-256 names.
    basetemp=ROOT/'artifacts'/'p'/uuid4().hex[:8]
    basetemp.parent.mkdir(parents=True,exist_ok=True)
    try:
        commands=[('concurrency',[sys.executable,'-m','pytest','tests/test_rag_concurrency.py','-q','--basetemp',str(basetemp)]),
                  ('smoke',[sys.executable,'-m','scripts.smoke_rag','--base-url',base_url,'--output',str(output/'smoke')])]
        for name,command in commands:
            result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=300)
            (output/(name+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
            report['checks'][name]={'exit_code':result.returncode,'log':name+'.log'}
            if result.returncode: return report
        report['passed']=True
        return report
    finally:
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description='第四章隔离并发验收')
    parser.add_argument('--mode',choices=('deterministic','real'),required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--base-url')
    args=parser.parse_args(argv)
    if args.mode=='deterministic' and args.base_url: raise ValueError('base URL is only valid in real mode')
    result=run_lease_probe(args.output) if args.mode=='deterministic' else (
        run_real_checks(args.output,args.base_url) if args.base_url else run_real_checks(args.output))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
