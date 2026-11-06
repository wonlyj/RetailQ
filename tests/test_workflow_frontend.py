from pathlib import Path
import shutil,subprocess
import pytest


@pytest.mark.parametrize('scenario',['validate','incomplete','handoff-cancel','handoff','ticket-cancel','dedupe','unknown','restore','restore-first','whitespace','stream-contract'])
def test_independent_actions_use_the_shipped_native_controller(scenario):
    node=shutil.which('node')
    if not node:
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if bundled.is_file():node=str(bundled)
    assert node,'Native JS action tests need the existing Node runtime.'
    result=subprocess.run([node,'--experimental-vm-modules','tests/support/workflow_frontend_races.mjs',scenario],
                          capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert 'PASS '+scenario in result.stdout
