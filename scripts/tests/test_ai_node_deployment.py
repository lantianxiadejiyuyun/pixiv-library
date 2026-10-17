from pathlib import Path
import subprocess
import sys

from scripts.ai_node import compose_command

ROOT = Path(__file__).resolve().parents[2]


def test_ai_node_commands_use_separate_environment_and_project(tmp_path):
    result = compose_command(['--intel', 'up', '-d', '--build'], root=tmp_path)
    assert result[-3:] == ['up', '-d', '--build']
    assert str(tmp_path/'deploy/ai-node.env') in result
    assert str(tmp_path/'deploy/compose.ai-node.intel.yaml') in result
    assert str(tmp_path/'deploy/compose.yaml') not in result
    assert compose_command(['--nvidia', 'ps'], root=tmp_path)[-3:] == ['-f', str(tmp_path/'deploy/compose.ai-node.nvidia.yaml'), 'ps']


def test_generated_controller_key_is_private_not_printed_and_never_overwritten(tmp_path):
    target = tmp_path/'ai-node.env'
    command = [sys.executable, str(ROOT/'scripts/bootstrap_ai_node.py'), '--output', str(target)]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    values = dict(line.split('=', 1) for line in target.read_text().splitlines() if line and not line.startswith('#'))
    key = values['QIYE_CONTROLLER_KEY']
    assert len(key) >= 43 and key not in first.stdout + first.stderr
    assert values['QIYE_NODE_ID'] and 'OLLAMA_IMAGE' in values
    original = target.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0 and target.read_bytes() == original
    assert key not in second.stdout + second.stderr


def test_deployment_has_only_controller_port_and_scoped_ai_labels():
    import yaml
    data = yaml.safe_load((ROOT/'deploy/compose.ai-node.yaml').read_text())
    services = data['services']
    assert set(services) == {'controller', 'ollama', 'vision'}
    assert len(services['controller']['ports']) == 1
    assert not services['ollama'].get('ports') and not services['vision'].get('ports')
    for name in ('ollama', 'vision'):
        assert services[name]['labels']['io.qiye.ai.service'] == name
        assert services[name]['labels']['io.qiye.ai.node'] == '${QIYE_NODE_ID}'
    assert '/var/run/docker.sock:/var/run/docker.sock' in services['controller']['volumes']
    assert not any('docker.sock' in mount for mount in services['vision']['volumes'])
