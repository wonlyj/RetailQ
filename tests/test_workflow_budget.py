import pytest


def test_reservation_rejects_overdraw_before_external_call():
    from retailq.workflow.budget import BudgetLedger, BudgetExceeded
    ledger = BudgetLedger(100)
    ledger.reserve(60,30,stage='intent')
    with pytest.raises(BudgetExceeded): ledger.reserve(5,6,stage='agent')
    assert ledger.remaining_output(5,20) == 5


def test_provider_usage_replaces_reservation_without_double_charging():
    from retailq.workflow.budget import BudgetLedger
    from retailq.workflow.contracts import UsageCharge
    ledger = BudgetLedger(200)
    reservation = ledger.reserve(100,50,stage='agent')
    ledger.settle(reservation,UsageCharge(40,10,'provider'))
    assert ledger.remaining_output(0,200) == 150
    with pytest.raises(ValueError): ledger.settle(reservation,UsageCharge(40,10,'provider'))


def test_missing_usage_keeps_full_reservation_across_state_roundtrip():
    from retailq.workflow.budget import BudgetLedger
    ledger = BudgetLedger(200)
    reservation = ledger.reserve(100,50,stage='retrieval')
    ledger.settle(reservation,None)
    recovered = BudgetLedger(200,ledger.snapshot())
    assert recovered.remaining_output(0,200) == 50
    assert recovered.snapshot()['entries'][0]['source'] == 'reserved'


@pytest.mark.parametrize('input_cost,output_cap',[(-1,2),(1,0),(True,1),(1,1.5)])
def test_invalid_estimates_cannot_increase_budget(input_cost,output_cap):
    from retailq.workflow.budget import BudgetLedger
    with pytest.raises(ValueError): BudgetLedger(200).reserve(input_cost,output_cap,stage='agent')
