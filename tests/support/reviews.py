"""Fixtures for real review records; external model doubles are added as needed."""
from datetime import datetime, timezone, timedelta

from retailq.knowledge.contracts import ConversationSnapshot, MiningMessage

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone(timedelta(hours=8)))
RID = 'a' * 32


def make_store(root):
    from retailq.knowledge.review_store import ReviewStore
    return ReviewStore(root, clock=lambda: NOW)


def make_source():
    return ConversationSnapshot('conversation:1:message:2', 1, 2,
                                (MiningMessage(1, 'user', '怎么清洁？'),
                                 MiningMessage(2, 'assistant', '擦干后阴凉晾干。')))


def make_run(run_id=RID):
    from retailq.knowledge.review_contracts import RunRecord
    return RunRecord(run_id=run_id, rule_hash='c' * 64, sources=(make_source(),),
                     created_at=NOW.isoformat(), updated_at=NOW.isoformat())


def make_operation(key=RID, ids=(1,)):
    from retailq.knowledge.review_contracts import (
        OperationRecord, PrepareCommand, SelectionSummary, canonical_request_hash,
    )
    command = PrepareCommand(request_key=key, conversation_ids=ids)
    return OperationRecord(operation_id=key, run_id=key, kind='prepare',
                           request_hash=canonical_request_hash(command), command=command,
                           source_snapshot=(make_source(),),
                           selection=SelectionSummary(eligible_total=1, selected=1, remaining=0),
                           rule_hash='c' * 64, accepted_at=NOW.isoformat())
