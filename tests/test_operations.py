import json
import re
import time

import requests
from click.testing import CliRunner
from yaml import safe_load

from gravity.cli import galaxyctl

STARTUP_TIMEOUT = 20


def test_cmd_register(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'register', str(galaxy_yml)])
    assert result.exit_code == 0
    assert 'Registered galaxy config:' in result.output


def test_cmd_deregister(state_dir, galaxy_yml):
    test_cmd_register(state_dir, galaxy_yml)
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'deregister', str(galaxy_yml)])
    assert result.exit_code == 0
    assert 'Deregistered config:' in result.output


def wait_for_startup(state_dir, free_port, prefix="/"):
    for _ in range(STARTUP_TIMEOUT * 4):
        try:
            requests.get(f"http://localhost:{free_port}{prefix}api/version").raise_for_status()
            return True
        except Exception:
            time.sleep(0.25)
    with open(state_dir / "log" / 'gunicorn.log') as fh:
        startup_logs = fh.read()
    return startup_logs


def start_instance(state_dir, free_port):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'start'])
    assert re.search(r"gunicorn\s*STARTING", result.output)
    assert result.exit_code == 0
    startup_done = wait_for_startup(state_dir, free_port)
    assert startup_done is True, f"Startup failed. Application startup logs:\n {startup_done}"


def test_cmd_start(state_dir, galaxy_yml, startup_config, free_port):
    galaxy_yml.write(json.dumps(startup_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'register', str(galaxy_yml)])
    assert result.exit_code == 0
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'update'])
    assert result.exit_code == 0
    start_instance(state_dir, free_port)
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'stop'])
    assert result.exit_code == 0
    assert "All processes stopped, supervisord will exit" in result.output


def test_cmd_restart_with_update(state_dir, galaxy_yml, startup_config, free_port):
    galaxy_yml.write(json.dumps(startup_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'register', str(galaxy_yml)])
    assert result.exit_code == 0
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'update'])
    assert result.exit_code == 0
    start_instance(state_dir, free_port)
    # change prefix
    prefix = '/galaxypf/'
    startup_config['galaxy']['galaxy_url_prefix'] = prefix
    galaxy_yml.write(json.dumps(startup_config))
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'restart'])
    assert result.exit_code == 0
    startup_done = wait_for_startup(state_dir=state_dir, free_port=free_port, prefix=prefix)
    assert startup_done is True, f"Startup failed. Application startup logs:\n {startup_done}"


def test_cmd_show(state_dir, galaxy_yml):
    test_cmd_register(state_dir, galaxy_yml)
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'show', str(galaxy_yml)])
    assert result.exit_code == 0
    details = safe_load(result.output)
    assert details['config_type'] == 'galaxy'


def test_cmd_show_config_does_not_exist(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'show', str(galaxy_yml)])
    assert result.exit_code == 1
    assert f"{str(galaxy_yml)} is not a registered config file." in result.output
    assert "No config files have been registered." in result.output
    assert "Registered config files are:" not in result.output
    assert f'To register this config file run "galaxyctl register {str(galaxy_yml)}"' in result.output
    # register the sample file, but ask for galaxy.yml
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'register', str(galaxy_yml + '.sample')])
    assert result.exit_code == 0
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'show', str(galaxy_yml)])
    assert result.exit_code == 1
    assert f"{str(galaxy_yml)} is not a registered config file." in result.output
    assert "Registered config files are:" in result.output
    assert f'To register this config file run "galaxyctl register {str(galaxy_yml)}"'


def test_cmd_instances(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'instances'])
    assert result.exit_code == 0
    assert not result.output
    test_cmd_register(state_dir, galaxy_yml)
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'instances'])
    assert result.exit_code == 0
    assert "_default_" in result.output


def test_cmd_configs(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'configs'])
    assert result.exit_code == 0
    assert 'No config files registered' in result.output
    test_cmd_register(state_dir, galaxy_yml)
    result = runner.invoke(galaxyctl, ['--state-dir', state_dir, 'configs'])
    assert result.exit_code == 0
    assert result.output.startswith("TYPE")
    assert str(galaxy_yml) in result.output