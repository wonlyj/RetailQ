"""Controlled response ordering against the shipped JS controller, not its copy."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('scenario',['old-poll','old-post-success','old-post-error','old-post-unknown'])
def test_stale_response_cannot_forget_the_new_seal_intent(scenario):
    node=shutil.which('node')
    if not node:
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if bundled.is_file(): node=str(bundled)
    assert node,'JS response-order regressions require existing Node.js (Codex bundled Node is supported).'
    result=subprocess.run([node,'--experimental-vm-modules','tests/support/review_frontend_races.mjs',scenario],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert 'PASS '+scenario in result.stdout
