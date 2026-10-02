"""Topology discovery exercises read-only boundaries, not deployment copies."""
import json
import subprocess
from pathlib import Path

import pytest

from test_installer_ui import ui_env  # noqa: F401
from test_installer_acceptance import ADDON_ROOT, FakeHaApi, _read_calls
from test_wizard_acceptance import (
    _seed_fresh_target, _wizard_env, _write_answers, _run_wizard,
    _assert_no_remote_writes, release_dir, HAS_PWSH,
)


def probe(ui_env):
    status, response = ui_env.client.post('/api/probe', {'names': ['topology']})
    assert status == 200
    ui_env.assert_no_remote_writes()
    return response['results']['topology']


def test_common_haos_detects_without_advanced_answers(ui_env, monkeypatch):
    ui_env.set_answers(ssh_host='', ssh_user='', ssh_port=22,
                       appdaemon_config_root=None, appdaemon_http_url=None,
                       ha_config_filesystem_confirmed=False)
    monkeypatch.setattr(ui_env.module, 'tcp_reachable', lambda host, port: host == '127.0.0.1' and port == 5050)
    result = probe(ui_env)
    assert result['status'] == 'ok'
    assert result['data']['advanced_required'] is False
    topology = result['data']['topology']
    assert topology['filesystem_host'] == '127.0.0.1'
    assert topology['appdaemon_config_root'] == ADDON_ROOT
    assert topology['appdaemon_http_url'] == 'http://127.0.0.1:5050'
    assert topology['capabilities']['ha_www_access_available'] is True
    calls = _read_calls(ui_env.state_dir)
    assert any('127.0.0.1' in call['argv'] and '22' in call['argv'] for call in calls)


def test_ssh_unavailable_offers_advanced_and_never_writes(ui_env):
    ui_env.set_answers()
    (ui_env.state_dir / 'ssh_unreachable').touch()
    result = probe(ui_env)
    assert result['data']['advanced_required'] is True
    assert 'Configure advanced topology' in result['detail']
    assert not result['data']['topology']['capabilities']['ha_www_access_available']


def test_multiple_candidates_require_selection(ui_env):
    ui_env.set_answers(appdaemon_config_root=None)
    (ui_env.state_dir / 'fs/addon_configs/second_appdaemon/apps').mkdir(parents=True)
    result = probe(ui_env)
    assert result['data']['advanced_required'] is True
    assert ui_env.session.answers['appdaemon_config_root'] is None
    assert len(ui_env.session.probe_results['appdaemon_root']['data']['candidates']) == 2
    ui_env.set_answers(addon_root_choice=2, appdaemon_config_root=None)
    probe(ui_env)
    assert ui_env.session.answers['appdaemon_config_root'].endswith('second_appdaemon')


def test_custom_host_port_http_remain_independent(ui_env, monkeypatch):
    ui_env.set_answers(ssh_host='custom-files', ssh_user='operator', ssh_port=2228,
                       appdaemon_http_url='https://custom-http.example:8443')
    seen = []
    monkeypatch.setattr(ui_env.module, 'tcp_reachable', lambda host, port: seen.append((host, port)) or True)
    topology = probe(ui_env)['data']['topology']
    assert topology['filesystem_host'] == 'custom-files'
    assert topology['filesystem_port'] == 2228
    assert seen == [('custom-http.example', 8443)]
    assert any('operator@custom-files' in c['argv'] and '2228' in c['argv'] for c in _read_calls(ui_env.state_dir))


@pytest.mark.parametrize('confirmed,markers,available', [(False, True, False), (True, False, False), (True, True, True)])
def test_split_card_requires_binding_and_ha_markers(ui_env, confirmed, markers, available):
    ui_env.set_answers(ssh_host='split-appdaemon', ha_config_filesystem_confirmed=confirmed)
    if not markers:
        (ui_env.state_dir / 'fs/config/.HA_VERSION').unlink()
    result = probe(ui_env)
    assert result['data']['topology']['capabilities']['ha_www_access_available'] is available
    _, plan = ui_env.client.post('/api/plan', {})
    assert plan['ok'] is True  # AppDaemon installation remains supported.
    assert plan['card_auto_available'] is available
    assert 'manual' in ui_env.session.probe_results['ha_www']['detail'].lower() or available


def test_same_machine_never_implies_local_transport(ui_env, monkeypatch):
    ui_env.set_answers(ssh_host='127.0.0.1', ha_config_filesystem_confirmed=False)
    monkeypatch.setattr(ui_env.module, 'tcp_reachable', lambda *_: True)
    topology = probe(ui_env)['data']['topology']
    assert topology['filesystem_transport'] == 'ssh'
    assert topology['capabilities']['local_filesystem_supported'] is False


def test_manual_restart_is_reported_when_supervisor_absent(ui_env):
    with FakeHaApi(supervisor_available=False) as api:
        ui_env.set_answers(ha_url=f'http://127.0.0.1:{api.port}')
        result = probe(ui_env)
    assert result['data']['advanced_required'] is True
    assert result['data']['topology']['appdaemon_restart_strategy'] == 'manual'


def test_common_detection_reads_upgrade_mode_before_skipping_directory_step(ui_env, monkeypatch):
    ui_env.set_answers(ssh_host='', appdaemon_config_root=None, appdaemon_http_url=None)
    adapter = ui_env.state_dir / 'fs' / ADDON_ROOT.strip('/') / 'apps/scene_studio/appdaemon_adapter/adapter.py'
    adapter.parent.mkdir(parents=True)
    adapter.write_text('# existing healthy adapter\n', encoding='utf-8')
    (ui_env.state_dir / 'apps_mode').write_text('normal', encoding='utf-8')
    monkeypatch.setattr(ui_env.module, 'tcp_reachable', lambda *_: True)
    assert probe(ui_env)['status'] == 'ok'
    _, plan = ui_env.client.post('/api/plan', {})
    assert plan['install_kind'] == 'upgrade'
    assert plan['profile']['runtime_mode'] == 'normal'


@pytest.mark.parametrize('patch', [{'ssh_port': 0}, {'ha_config_filesystem_confirmed': 'false'},
                                  {'appdaemon_http_url': 'http://user:credential@example.test:5050'}])
def test_invalid_topology_answers_are_not_persisted(ui_env, patch):
    ui_env.set_answers()
    before = ui_env.session.public_answers()
    code, result = ui_env.client.post('/api/answers', patch)
    assert code == 400
    assert 'user:credential@' not in json.dumps(result)
    assert ui_env.session.public_answers() == before


def test_changing_host_invalidates_card_capability(ui_env):
    ui_env.set_answers()
    assert probe(ui_env)['data']['topology']['capabilities']['ha_www_access_available'] is True
    ui_env.set_answers(ssh_host='another-host', ha_config_filesystem_confirmed=False)
    assert ui_env.session.topology_snapshot()['capabilities']['ha_www_access_available'] is False


def test_browser_common_path_hides_advanced_then_exposes_failed_detection(ui_env, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    playwright = root / 'home-assistant/scene-studio-card/node_modules/playwright/index.mjs'
    if not playwright.is_file():
        pytest.skip('Install card npm dependencies to run installer browser acceptance')
    ui_env.set_answers(ssh_host='', ssh_user='', appdaemon_config_root=None,
                       appdaemon_http_url=None, ha_config_filesystem_confirmed=False)
    monkeypatch.setattr(ui_env.module, 'tcp_reachable', lambda host, port: port == 5050)
    assert probe(ui_env)['status'] == 'ok'
    script = Path(__file__).with_name('browser_topology.mjs')
    result = subprocess.run(['node', str(script),
        f'http://127.0.0.1:{ui_env.client.port}/?token={ui_env.client.token}', str(playwright)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    ui_env.assert_no_remote_writes()


@pytest.mark.skipif(not HAS_PWSH, reason='CLI acceptance requires PowerShell 7')
def test_common_cli_unattended_defaults_no_advanced_prompts(release_dir, tmp_path):
    state_dir = tmp_path / 'fake'
    _seed_fresh_target(state_dir)
    try:
        http = FakeHaApi(port=5050)
    except OSError:
        pytest.skip('localhost :5050 is occupied')
    with http, FakeHaApi() as ha:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(tmp_path / 'answers.json', ha_url=f'http://127.0.0.1:{ha.port}',
            ssh_host=..., ssh_user=..., ssh_port=..., appdaemon_config_root=...,
            appdaemon_http_url=..., confirm_install=False, ha_config_filesystem_confirmed=False)
        result = _run_wizard(release_dir, answers, env, yes=False)
        # Interactive flow: only ordinary questions may be asked. Any SSH,
        # config-root or HTTP question fails this real wizard invocation.
        interactive = tmp_path / 'interactive.ps1'
        wizard_path = str(release_dir / 'Install-SceneStudio.ps1').replace("'", "''")
        interactive.write_text(f"""
function Read-Host {{
    param([string]$Prompt, [switch]$AsSecureString)
    if ($Prompt -eq 'Press Enter to begin') {{ return '' }}
    if ($Prompt.StartsWith('Home Assistant API address')) {{ return 'http://127.0.0.1:{ha.port}' }}
    if ($Prompt.StartsWith('Home Assistant lights')) {{ return '' }}
    if ($Prompt -match '^(Philips Hue|WLED|hyperHDR integration|Install the Scene Studio Home Assistant dashboard card)') {{ return 'n' }}
    if ($Prompt.StartsWith('Scene Studio data directory')) {{ return '' }}
    if ($Prompt.StartsWith('Type INSTALL')) {{ return 'CANCEL' }}
    throw "Unexpected advanced prompt: $Prompt"
}}
. '{wizard_path}'
""", encoding='utf-8')
        interactive_result = subprocess.run(['pwsh', '-NoProfile', '-File', str(interactive)],
            env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Detected setup:' in result.stdout
    assert 'Configure advanced topology' not in result.stdout
    assert 'REVIEW ONLY' in result.stdout
    assert 'http://127.0.0.1:5050' in result.stdout
    assert '127.0.0.1' in json.dumps(_read_calls(state_dir))
    assert 'Unexpected advanced prompt' not in interactive_result.stdout
    assert 'Cancelled - nothing was changed.' in interactive_result.stdout, interactive_result.stdout + interactive_result.stderr
    _assert_no_remote_writes(state_dir)


@pytest.mark.skipif(not HAS_PWSH, reason='CLI acceptance requires PowerShell 7')
def test_split_cli_installs_product_but_skips_card(release_dir, tmp_path):
    state_dir = tmp_path / 'fake'
    _seed_fresh_target(state_dir)
    with FakeHaApi() as ha:
        answers = _write_answers(tmp_path / 'answers.json', ha_url=f'http://127.0.0.1:{ha.port}',
            appdaemon_http_url=f'http://appdaemon.example.test:{ha.port}',
            ssh_host='split-appdaemon', ha_config_filesystem_confirmed=False)
        result = _run_wizard(release_dir, answers, _wizard_env(tmp_path, state_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state_dir / 'fs' / ADDON_ROOT.strip('/') / 'apps/scene_studio/appdaemon_adapter/adapter.py').is_file()
    assert not (state_dir / 'fs/config/www/scene-studio-card').exists()
    assert 'Manual deployment:' in result.stdout


@pytest.mark.skipif(not HAS_PWSH, reason='CLI acceptance requires PowerShell 7')
def test_supervisor_missing_blocks_before_configuration_writes(release_dir, tmp_path):
    state_dir = tmp_path / 'fake'
    _seed_fresh_target(state_dir)
    with FakeHaApi(supervisor_available=False) as ha:
        answers = _write_answers(tmp_path / 'answers.json', ha_url=f'http://127.0.0.1:{ha.port}',
            appdaemon_http_url=f'http://appdaemon.example.test:{ha.port}')
        result = _run_wizard(release_dir, answers, _wizard_env(tmp_path, state_dir))
    assert result.returncode != 0
    assert 'Manual restart targets are not supported' in result.stdout
    _assert_no_remote_writes(state_dir)
