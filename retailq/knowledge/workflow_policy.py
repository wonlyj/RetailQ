from hashlib import sha256
from pathlib import Path
from retailq.errors import ServiceError
from retailq.knowledge.answer import ConfidencePolicy
from retailq.knowledge.policy import policy_identity

ROOT=Path(__file__).resolve().parents[2]
MODES=('policy_single','policy_multi')


def workflow_policy_identity(settings,snapshot,mode):
    if mode not in MODES:raise ValueError('unknown policy mode')
    identity=policy_identity(settings,snapshot,'all',version=2)
    paths=['retailq/workflow/query_processing.py','retailq/workflow/router_contracts.py',
           'retailq/prompts/workflow_resolve.txt','retailq/prompts/workflow_query_plan.txt',
           'retailq/prompts/workflow_expand.txt','retailq/knowledge/policy_retrieval.py',
           'retailq/knowledge/workflow_policy.py','scripts/calibrate_router.py']
    identity['query_hash']=sha256(identity['query_hash'].encode()+b'\0'+b'\0'.join((ROOT/p).read_bytes() for p in paths)).hexdigest()
    identity['strategy']=mode
    return identity


def freeze_workflow_policy(settings,snapshot,mode):
    if mode not in MODES:raise ValueError('unknown policy mode')
    path=Path(getattr(settings,'workflow_'+mode+'_confidence_path'))
    if not path.is_absolute():path=ROOT/path
    try:raw=path.read_bytes()
    except OSError:raise ServiceError('rag_policy_unavailable',503,'请先完成第六章政策检索校准。') from None
    return ConfidencePolicy.from_bytes(raw,expected_identity=workflow_policy_identity(settings,snapshot,mode)),sha256(raw).hexdigest()
