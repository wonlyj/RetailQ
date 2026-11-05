"""Reserve before external calls; unknown usage never refunds the reservation."""
from dataclasses import dataclass
from copy import deepcopy
from uuid import uuid4

from retailq.workflow.contracts import UsageCharge


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    stage: str
    input_estimate: int
    output_cap: int


class BudgetLedger:
    def __init__(self, limit: int, snapshot: dict | None = None):
        if type(limit) is not int or limit <= 0: raise ValueError('invalid limit')
        self.limit = limit
        self.entries = deepcopy(snapshot['entries']) if snapshot else []

    @property
    def spent(self):
        return sum(e['input_tokens'] + e['output_tokens'] for e in self.entries)

    def remaining_output(self, input_estimate: int, desired: int) -> int:
        return max(0, min(desired, self.limit - self.spent - input_estimate))

    def reserve(self, input_estimate: int, output_cap: int, *, stage: str) -> Reservation:
        if type(input_estimate) is not int or type(output_cap) is not int or input_estimate < 0 or output_cap < 1:
            raise ValueError('invalid reservation')
        if input_estimate + output_cap + self.spent > self.limit: raise BudgetExceeded()
        result = Reservation(uuid4().hex, stage, input_estimate, output_cap)
        self.entries.append({'id':result.reservation_id, 'stage':stage, 'input_tokens':input_estimate,
                             'output_tokens':output_cap, 'source':'reserved', 'settled':False})
        return result

    def settle(self, reservation: Reservation, charge: UsageCharge | None) -> None:
        entry = next(e for e in self.entries if e['id'] == reservation.reservation_id)
        if entry['settled']: raise ValueError('reservation already settled')
        if charge is not None:
            if (type(charge.input_tokens) is not int or type(charge.output_tokens) is not int
                    or min(charge.input_tokens,charge.output_tokens) < 0):
                raise ValueError('invalid charge')
            entry.update(input_tokens=charge.input_tokens, output_tokens=charge.output_tokens, source=charge.source)
        entry['settled'] = True

    def snapshot(self) -> dict:
        return {'limit':self.limit, 'entries':deepcopy(self.entries)}
