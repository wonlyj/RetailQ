from html.parser import HTMLParser
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient

from retailq.config import Settings
from retailq.errors import ServiceError
from tests.test_review_api import api_env


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.targets = []
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.targets.extend(value for name, value in attrs if name == 'href')


def test_console_page_and_navigation_exist(api_env):
    client = api_env.client
    page = client.get('/knowledge')
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-cache'
    assert '/knowledge' in Links(client.get('/').text).targets
    assert '/' in Links(page.text).targets
    assert client.get('/assets/knowledge.js').status_code == 200


def test_console_page_opens_when_knowledge_is_unconfigured():
    from retailq.app import create_app
    with TestClient(create_app(Settings(_env_file=None)), base_url='http://127.0.0.1', client=('127.0.0.1',50000)) as client:
        assert client.get('/knowledge').status_code == 200
        assert client.get('/api/knowledge/summary').status_code == 503
        assert client.get('/knowledge', headers={'Host':'evil.example'}).status_code == 403


def test_console_server_scope_rejects_demo_and_path_escape(tmp_path):
    from tests.support.review_console_server import validate_console_scope
    root = Path('artifacts/verification/ch03-review') / ('browser-unit-' + uuid.uuid4().hex)
    good = Settings(_env_file=None, mysql_host='127.0.0.1', mysql_port=3307, mysql_database='retailq_test')
    assert validate_console_scope(good, root).name == root.name
    with pytest.raises(ServiceError):
        validate_console_scope(good.model_copy(update={'mysql_port':3306, 'mysql_database':'retailq'}), root)
    with pytest.raises(ServiceError):
        validate_console_scope(good, tmp_path)


def test_database_reset_scope_is_checked_before_connect(monkeypatch):
    from tests.support.database import reset_isolated_database
    import retailq.db
    def forbidden(*args):
        raise AssertionError('must reject unsafe scope before constructing Database')
    monkeypatch.setattr(retailq.db, 'Database', forbidden)
    with pytest.raises(ServiceError):
        reset_isolated_database(Settings(_env_file=None, mysql_port=3306, mysql_database='retailq'))


def test_fixture_stop_file_drains_lifespan(free_tcp_port):
    import subprocess
    import sys
    import httpx
    root = Path.cwd()
    output = root / 'artifacts/verification/ch03-review' / ('browser-stop-unit-' + uuid.uuid4().hex)
    process = subprocess.Popen([sys.executable,'-m','tests.support.review_console_server','--scope','test',
        '--output',str(output),'--port',str(free_tcp_port)],cwd=root,stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
    lines = []
    try:
        for line in process.stdout:
            lines.append(line)
            if 'Uvicorn running on' in line:
                break
        assert any('Uvicorn running on' in line for line in lines), ''.join(lines)
        with httpx.Client(trust_env=False,timeout=3) as client:
            assert client.get(f'http://127.0.0.1:{free_tcp_port}/knowledge').status_code == 200
        (output/'stop.request').write_text('stop',encoding='ascii')
        try:
            rest, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail('fixture server ignored its explicit stop file')
        assert process.returncode == 0
        assert 'Application shutdown complete.' in rest
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
