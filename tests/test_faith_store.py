import pytest
from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.errors import ServiceError
from retailq.knowledge.faith_store import FaithStore


def test_active_run_survives_process_lease_and_requires_resume_or_seal(tmp_path):
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge'))
    store = FaithStore(tmp_path/'faith', coordinator)
    store.begin('run1', {'version':'1'})
    store.close()
    other = FaithStore(tmp_path/'faith', coordinator)
    with pytest.raises(ServiceError):
        other.begin('run2', {'version':'1'})
    other.resume('run1')
    other.seal('run1', '中断并保留审计')
    other.begin('run2', {'version':'1'})
    other.complete('run2')


def test_terminal_receipt_survives_crash_before_active_pointer_removal(tmp_path):
    from retailq.knowledge.corpus import _write
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge'))
    store = FaithStore(tmp_path/'faith', coordinator)
    store.begin('run1', {'version':'1'})
    state = store._read()
    _write(store.root/'run1.json', dict(state, status='sealed', reason='中断'))
    store.close()
    recovered = FaithStore(tmp_path/'faith', coordinator)
    with pytest.raises(ServiceError): recovered.resume('run1')
    recovered.begin('run2', {'version':'1'})
    recovered.complete('run2')
