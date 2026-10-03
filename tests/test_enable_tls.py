"""Meaningful orchestration checks without DNS/CA/Docker side effects."""

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest


MOCK_TOOL = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TLS_TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
mode = os.environ.get('TLS_TEST_DNS', 'ready')
if name == 'dig':
    if args[-1] == 'A':
        if mode == 'timeout':
            sys.exit(9)
        print('192.0.2.11' if mode == 'wrong' or (mode == 'split' and '@8.8.8.8' in args) else '192.0.2.10')
        if mode == 'multiple':
            print('192.0.2.12')
    elif mode == 'ipv6':
        print('2001:db8::10')
elif name == 'openssl':
    if os.environ.get('TLS_TEST_EXPIRED') and not pathlib.Path(os.environ['TLS_TEST_ISSUED']).exists():
        sys.exit(1)
elif name == 'certbot':
    if os.environ.get('TLS_TEST_CERTBOT_FAIL'):
        sys.exit(1)
    root = pathlib.Path(os.environ['TLS_TEST_CERT_ROOT']) / 'live' / args[args.index('-d') + 1]
    root.mkdir(parents=True, exist_ok=True)
    (root / 'fullchain.pem').write_text('test certificate')
    (root / 'privkey.pem').write_text('test key')
    pathlib.Path(os.environ['TLS_TEST_ISSUED']).touch()
elif name == 'docker':
    if args[:1] == ['inspect']:
        print('true')
    failure = os.environ.get('TLS_TEST_DOCKER_FAIL', '')
    if args[:1] == ['compose'] and failure and failure in args:
        sys.exit(1)
'''


@pytest.fixture
def tls_runner(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'scripts' / 'enable_tls.sh'
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.env').write_text('DOMAIN=example.org\nCERT_DIR=/etc/letsencrypt\n')
    (project / 'docker-compose.yml').write_text('name: hamzavpn\n')
    certificate_root = tmp_path / 'certificates'
    certificate_root.mkdir()
    script = tmp_path / 'enable_tls.sh'
    # Redirect the one fixed certificate directory into the test filesystem.
    script.write_text(source.read_text().replace('CERT_ROOT=/etc/letsencrypt', 'CERT_ROOT=' + shlex.quote(str(certificate_root))))
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ['dig', 'openssl', 'flock', 'docker', 'certbot']:
        tool = tools / name
        tool.write_text(MOCK_TOOL)
        tool.chmod(0o755)
    log = tmp_path / 'calls.jsonl'
    issued = tmp_path / 'issued'

    def run(*, valid_certificate=False, domain_line=None, **options):
        if valid_certificate:
            live = certificate_root / 'live' / 'example.org'
            live.mkdir(parents=True, exist_ok=True)
            (live / 'fullchain.pem').write_text('existing certificate')
            (live / 'privkey.pem').write_text('existing key')
        if domain_line is not None:
            (project / '.env').write_text(domain_line + '\nCERT_DIR=/etc/letsencrypt\n')
        log.write_text('')
        env = dict(os.environ, PROJECT_DIR=str(project), ORIGIN_IPV4='192.0.2.10',
                   PATH=str(tools) + os.pathsep + os.environ['PATH'],
                   TLS_TEST_LOG=str(log), TLS_TEST_CERT_ROOT=str(certificate_root),
                   TLS_TEST_ISSUED=str(issued), **options)
        result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True, timeout=10)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls, certificate_root, project

    return run


@pytest.mark.parametrize('mode', ['wrong', 'split', 'multiple', 'ipv6', 'timeout'])
def test_dns_gate_never_calls_ca_or_docker(tls_runner, mode):
    result, calls, _, _ = tls_runner(TLS_TEST_DNS=mode)
    assert result.returncode == 0
    assert result.stdout.strip() == 'TLS pending DNS'
    assert not any(call[0] in {'certbot', 'docker'} for call in calls)


def test_existing_certificate_skips_issuance_and_switches_only_project_listener(tls_runner):
    result, calls, root, _ = tls_runner(valid_certificate=True)
    assert result.returncode == 0, result.stderr
    assert 'TLS ready' in result.stdout
    assert not any(call[0] == 'certbot' for call in calls)
    assert os.readlink(root / 'fullchain.pem') == 'live/example.org/fullchain.pem'
    assert os.readlink(root / 'privkey.pem') == 'live/example.org/privkey.pem'
    config = next(i for i, call in enumerate(calls) if call[0] == 'docker' and 'run' in call)
    stop = calls.index(['docker', 'stop', 'hamzavpn_bootstrap'])
    up = next(i for i, call in enumerate(calls) if call[0] == 'docker' and 'up' in call)
    assert config < stop < up
    assert all(call[-1] == 'hamzavpn_bootstrap' for call in calls if call[:2] == ['docker', 'stop'])
    assert all('--no-deps' in call and call[-1] == 'nginx' for call in calls if call[0] == 'docker' and 'up' in call)


def test_new_certificate_uses_webroot_and_repeat_is_idempotent(tls_runner):
    result, calls, _, project = tls_runner()
    assert result.returncode == 0, result.stderr
    certbot = next(call for call in calls if call[0] == 'certbot')
    assert certbot[1:3] == ['certonly', '--webroot']
    assert certbot[certbot.index('-w') + 1] == str(project / 'certbot' / 'data')
    assert certbot[certbot.index('-d') + 1] == 'example.org'
    assert '--non-interactive' in certbot and '--agree-tos' in certbot
    result, calls, _, _ = tls_runner()
    assert result.returncode == 0
    assert not any(call[0] == 'certbot' for call in calls)


def test_expiring_certificate_is_reissued(tls_runner):
    result, calls, _, _ = tls_runner(valid_certificate=True, TLS_TEST_EXPIRED='1')
    assert result.returncode == 0, result.stderr
    assert sum(call[0] == 'certbot' for call in calls) == 1


def test_ca_failure_preserves_bootstrap(tls_runner):
    result, calls, _, _ = tls_runner(TLS_TEST_CERTBOT_FAIL='1')
    assert result.returncode != 0
    assert not any(call[0] == 'docker' for call in calls)


def test_nginx_config_failure_preserves_bootstrap(tls_runner):
    result, calls, _, _ = tls_runner(valid_certificate=True, TLS_TEST_DOCKER_FAIL='run')
    assert result.returncode != 0
    assert ['docker', 'stop', 'hamzavpn_bootstrap'] not in calls
    assert not any(call[0] == 'docker' and 'up' in call for call in calls)


@pytest.mark.parametrize('failure', ['up', 'exec'])
def test_nginx_start_failure_restores_bootstrap(tls_runner, failure):
    result, calls, _, _ = tls_runner(valid_certificate=True, TLS_TEST_DOCKER_FAIL=failure)
    assert result.returncode != 0
    assert ['docker', 'stop', 'hamzavpn_bootstrap'] in calls
    assert ['docker', 'start', 'hamzavpn_bootstrap'] in calls
    assert any(call[0] == 'docker' and call[-2:] == ['stop', 'nginx'] for call in calls)
    assert 'HTTP listener restored' in result.stderr


def test_dotenv_is_never_executed(tls_runner, tmp_path):
    marker = tmp_path / 'must-not-exist'
    result, calls, _, _ = tls_runner(domain_line=f'DOMAIN=$(touch {marker})')
    assert result.returncode != 0
    assert not marker.exists()
    assert not calls
