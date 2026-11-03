"""Report explicitly selected, reachable clients as a virtual UniFi switch.

This entry point has no provisioning, shell-command, upgrade or discovery handler.
It reuses the upstream Inform codec; the gateway entry point remains available.
"""
import argparse
import configparser
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from unifi_protocol import decode_inform, encode_inform

LOG = logging.getLogger('client-reporter')
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z')


class CompatibilityError(Exception):
    pass


def load_config(path):
    config = json.loads(Path(path).read_text())
    config['ip'] = str(ipaddress.IPv4Address(config['ip']))
    if not MAC.fullmatch(config['mac']) or not int(config['mac'][:2], 16) & 2:
        raise ValueError('Use a dedicated locally administered switch MAC')
    if config['mac'] in {t['mac'] for t in config['targets']}:
        raise ValueError('The reporter must not use a client MAC')
    if not config['targets'] or len(config['targets']) > 7:
        raise ValueError('Configure between one and seven explicit targets')
    for target in config['targets']:
        target['ip'] = str(ipaddress.IPv4Address(target['ip']))
        if not MAC.fullmatch(target['mac']):
            raise ValueError('Invalid target MAC')
    if len({t['mac'] for t in config['targets']}) != len(config['targets']):
        raise ValueError('Duplicate target MAC')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', config['interface']):
        raise ValueError('Invalid interface')
    return config


def probe(target, interface):
    # Binding the interface prevents a routed reply being assigned another MAC.
    result = subprocess.run(
        ['ping', '-n', '-q', '-I', interface, '-c', '1', '-W', '1', target['ip']],
        capture_output=True, timeout=3, check=False,
    )
    if result.returncode:
        return False
    result = subprocess.run(
        ['ip', '-j', '-4', 'neigh', 'show', 'to', target['ip'], 'dev', interface],
        capture_output=True, timeout=3, check=True,
    )
    return any(n.get('dst') == target['ip']
               and n.get('lladdr', '').lower() == target['mac']
               and not {'FAILED', 'INCOMPLETE'} & set(n.get('state', []))
               for n in json.loads(result.stdout))


def payload(config, online, adopted, cfgversion='', started=None):
    uptime = int(time.monotonic() - started) if started is not None else 0
    data = {
        'model': 'US8', 'version': '7.2.123.16020',
        'mac': config['mac'], 'serial': config['mac'].replace(':', ''),
        'ip': config['ip'], 'netmask': '255.255.255.0',
        'hostname': config['name'], 'inform_url': config['controller_url'],
        'time': int(time.time()), 'uptime': uptime,
        'default': not adopted, 'state': 2 if adopted else 0,
        'cfgversion': cfgversion, 'required_version': '4.3.0',
    }
    if adopted:
        # Ports describe observations only. No traffic counters are invented.
        ports = [{'port_idx': 1, 'name': 'Virtual uplink', 'up': True,
                  'is_uplink': True, 'enable': True, 'mac_table': []}]
        for idx, target in enumerate(config['targets'], 2):
            ports.append({
                'port_idx': idx, 'name': target['name'], 'enable': True,
                'is_uplink': False, 'up': target['mac'] in online,
                'mac_table': [{'mac': target['mac'], 'ip': target['ip'],
                               'vlan': 1, 'age': 0, 'static': False,
                               'uptime': 0}]
                if target['mac'] in online else [],
            })
        data['port_table'] = ports
        data['ethernet_table'] = [{'name': 'eth0', 'mac': config['mac'], 'num_port': 8}]
    return data


def apply_response(response, state):
    if not isinstance(response, dict):
        raise CompatibilityError('Unexpected controller response structure')
    kind = response.get('_type')
    if kind == 'noop':
        return dict(state)
    if kind != 'setparam':
        raise CompatibilityError(f'Unsupported controller response type: {kind}')
    if not isinstance(response.get('mgmt_cfg', ''), str):
        raise CompatibilityError('Unexpected adoption metadata structure')
    new = dict(state)
    # Only adoption/encryption metadata is accepted. system_cfg and every
    # provisioning or executable field are deliberately never applied.
    for line in response.get('mgmt_cfg', '').splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        if key == 'authkey':
            if not re.fullmatch('[0-9a-fA-F]{32}', value):
                raise CompatibilityError('Invalid adoption key')
            new['key'] = value
        elif key == 'use_aes_gcm':
            if value not in ('true', 'false'):
                raise CompatibilityError('Invalid encryption mode')
            new['gcm'] = value == 'true'
        elif key == 'cfgversion':
            new['cfgversion'] = value
    if new.get('key'):
        new['adopted'] = True
    return new


def codec_config(config, state):
    codec = configparser.ConfigParser()
    codec.read_dict({'gateway': {
        'lan_mac': config['mac'], 'is_adopted': str(state.get('adopted', False)),
        'key': state.get('key', ''), 'use_aes_gcm': str(state.get('gcm', False)),
    }})
    return codec


def save_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def run(config, state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = state_dir / 'adoption.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    started = time.monotonic()
    # Never forward reports or adoption keys to a redirect destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    while True:
        try:
            online = {t['mac'] for t in config['targets'] if probe(t, config['interface'])}
            codec = codec_config(config, state)
            body = payload(config, online, state.get('adopted', False),
                           state.get('cfgversion', ''), started)
            request = urllib.request.Request(
                config['controller_url'], data=encode_inform(codec, json.dumps(body)),
                headers={'Content-Type': 'application/x-binary'}, method='POST',
            )
            with opener.open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise CompatibilityError('Oversize controller response')
            try:
                decoded = decode_inform(codec, raw)
            except Exception as exc:
                raise CompatibilityError('Cannot decode controller response') from exc
            new_state = apply_response(decoded, state)
            if new_state != state:
                save_json(state_path, new_state)
                state = new_state
            save_json(state_dir / 'status.json', {
                'checked_at': int(time.time()), 'online': sorted(online),
                'adopted': state.get('adopted', False),
            })
            LOG.info('Reported %d/%d verified clients; adopted=%s',
                     len(online), len(config['targets']), state.get('adopted', False))
        except CompatibilityError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # No cached client list is sent after a failed observation.
            LOG.warning('No successful report: %s', type(exc).__name__)
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='/var/lib/unifi-client-reporter')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        run(load_config(args.config), Path(args.state_dir))
    except CompatibilityError as exc:
        LOG.error('Reporter stopped; network configuration is unaffected: %s', exc)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
