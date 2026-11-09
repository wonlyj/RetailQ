from types import SimpleNamespace
import pytest
from retailq.errors import ServiceError


def test_durable_budget_is_internal_and_cannot_reset():
    from retailq.workflow.pending import pending_from_snapshot,public_pending
    payload={'kind':'order_selector','conversation_id':1,'turn_id':'a'*32,'options':[],
             'message':'请选择订单','can_cancel':True,'pause_budget':{'remaining_active_seconds':30}}
    snapshot=SimpleNamespace(interrupts=(SimpleNamespace(id='interrupt1',value=payload),),values={'turn_id':'a'*32})
    pending=pending_from_snapshot(snapshot,120)
    assert pending['pause_budget']['remaining_active_seconds']==30
    assert 'pause_budget' not in public_pending(pending)
    payload['pause_budget']['remaining_active_seconds']=float('nan')
    with pytest.raises(ServiceError):pending_from_snapshot(snapshot,120)
