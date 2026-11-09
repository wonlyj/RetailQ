"""Controlled policy fixture, never evidence of real retrieval quality."""
import json
from types import SimpleNamespace
from retailq.knowledge.workflow_policy import workflow_policy_identity


def configure_policy(runtime,result,path,*,allowed=True):
    runtime.chat_repository.policy_retrieve=lambda q,*,queries,mode,deadline:SimpleNamespace(**{**vars(result),'strategy':mode})
    for mode in ('policy_single','policy_multi'):
        target=path.with_name(path.stem+'-'+mode+'.json')
        setattr(runtime.settings,'workflow_'+mode+'_confidence_path',str(target))
        target.write_text(json.dumps({'identity':workflow_policy_identity(runtime.settings,result.snapshot,mode),
            'calibrated':True,'thresholds':{mode:0 if allowed else 2}}),encoding='utf-8')
