import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from client_reporter import (
    CompatibilityError,
    apply_response,
    codec_config,
    load_config,
    main,
    payload,
    probe,
)
from unifi_protocol import decode_inform, encode_inform


@pytest.fixture
def config():
    return load_config('conf/client-reporter.sample.json')


def test_only_allowlisted_live_clients_are_reported(config):
    target = config['targets'][0]
    data = payload(config, {target['mac'], 'aa:bb:cc:dd:ee:ff'}, True)
    reported = [x['mac'] for p in data['port_table'] for x in p['mac_table']]
    assert reported == [target['mac']]
    assert data['model'] == 'US8'
    assert 'network_table' not in data
    assert 'routes' not in data
    assert not any(p['mac_table'] for p in payload(config, set(), True)['port_table'])


@pytest.mark.parametrize('reachable,observed,expected', [
    (False, '00:11:22:33:44:55', False),
    (True, 'aa:bb:cc:dd:ee:ff', False),
    (True, '00:11:22:33:44:55', True),
])
def test_probe_requires_ping_and_correct_mac(config, reachable, observed, expected):
    target = config['targets'][0]
    ping = Mock(returncode=0 if reachable else 1)
    neigh = Mock(stdout=json.dumps([{
        'dst': target['ip'], 'lladdr': observed, 'state': ['REACHABLE'],
    }]))
    with patch('client_reporter.subprocess.run', side_effect=[ping, neigh]) as run:
        assert probe(target, 'eth0') is expected
    assert all(call.kwargs.get('shell', False) is False for call in run.call_args_list)


@pytest.mark.parametrize('kind', ['cmd', 'reboot', 'upgrade', 'setdefault', 'future-type'])
def test_controller_commands_stop_without_execution(kind):
    state = {'adopted': True, 'key': 'a' * 32}
    with patch('client_reporter.subprocess.run') as run, pytest.raises(CompatibilityError):
        apply_response({'_type': kind, 'cmd': 'reboot'}, state)
    run.assert_not_called()
    assert state == {'adopted': True, 'key': 'a' * 32}


def test_provisioning_is_never_applied():
    response = {
        '_type': 'setparam', 'system_cfg': 'run arbitrary command',
        'mgmt_cfg': 'authkey=' + 'b' * 32 + '\nuse_aes_gcm=true\n'
                    'cfgversion=123\nmgmt_url=http://elsewhere/\n',
    }
    assert apply_response(response, {}) == {
        'key': 'b' * 32, 'gcm': True, 'cfgversion': '123', 'adopted': True,
    }


@pytest.mark.parametrize('gcm', [False, True])
def test_codec_roundtrip_and_corruption(config, gcm):
    state = {'adopted': True, 'key': 'c' * 32, 'gcm': gcm}
    codec = codec_config(config, state)
    data = payload(config, set(), True)
    encoded = encode_inform(codec, json.dumps(data))
    assert decode_inform(codec, encoded) == data
    with pytest.raises(ValueError):
        decode_inform(codec, encoded[:-5])


def test_native_gateway_codec_still_works(config):
    codec = codec_config(config, {})
    data = {'model': 'UGW3', 'network_table': []}
    assert decode_inform(codec, encode_inform(codec, json.dumps(data))) == data


def test_incompatible_controller_has_nonrestarting_exit_status(config):
    with patch('sys.argv', ['client_reporter.py', '--config', 'unused']), \
            patch('client_reporter.load_config', return_value=config), \
            patch('client_reporter.run', side_effect=CompatibilityError('new format')):
        assert main() == 78


@pytest.mark.parametrize('response', [None, [], {'_type': 'setparam', 'mgmt_cfg': []}])
def test_changed_controller_schema_stops_cleanly(response):
    with pytest.raises(CompatibilityError):
        apply_response(response, {})


@pytest.mark.parametrize('reply', ['reboot', 'corrupt'])
def test_real_process_stops_on_incompatible_controller(config, tmp_path, reply):
    class Controller(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = (encode_inform(codec_config(config, {}), '{"_type":"reboot"}')
                    if reply == 'reboot' else b'incompatible response')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Controller)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        config['controller_url'] = f'http://127.0.0.1:{server.server_port}/inform'
        config['interface'] = 'lo'
        config['targets'][0]['ip'] = '127.0.0.1'
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(config))
        result = subprocess.run([
            sys.executable, str(Path(__file__).parents[1] / 'client_reporter.py'),
            '--config', str(path), '--state-dir', str(tmp_path / 'state'),
        ], capture_output=True, timeout=10, check=False)
        assert result.returncode == 78, result.stderr.decode()
        assert not (tmp_path / 'state' / 'status.json').exists()
        assert not (tmp_path / 'state' / 'adoption.json').exists()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
