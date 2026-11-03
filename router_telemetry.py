"""Publish read-only OpenWrt observations using the safe Inform transport."""
import argparse
import copy
import ipaddress
import json
import logging
import os
from pathlib import Path
import subprocess
import time

from client_reporter import CompatibilityError, MAC, run


def read_snapshot(config):
    if config.get('snapshot_file'):
        snapshot = json.loads(Path(config['snapshot_file']).read_text())
    else:
        snapshot = _ssh_snapshot(config)
    if snapshot.get('schema_version') != 1:
        raise CompatibilityError('Unsupported OpenWrt snapshot schema')
    age = time.time() - snapshot['collected_at']
    if not -5 <= age <= 60:
        raise ValueError('OpenWrt snapshot is not fresh')
    lan = next(i for i in snapshot['interfaces'] if i['role'] == 'lan')
    if lan['mac'] != config['mac']:
        raise CompatibilityError('Router identity does not match configuration')
    return snapshot


def _ssh_snapshot(config):
    result = subprocess.run([
        'ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=' + config['known_hosts'],
        '-i', config['ssh_key'], 'root@' + str(ipaddress.IPv4Address(config['router_ip'])),
    ], capture_output=True, timeout=25, check=True)
    return json.loads(result.stdout)


def gateway_payload(config, snapshot, state, previous=None):
    lan = next(i for i in snapshot['interfaces'] if i['role'] == 'lan')
    address = lan['addresses'][0]
    data = {
        'model': 'UGW3', 'model_display': 'OpenWrt telemetry',
        'version': '4.4.57.5578372', 'required_version': '4.4.0',
        'mac': config['mac'], 'serial': config['mac'].replace(':', ''),
        'ip': address['address'], 'netmask': str(ipaddress.IPv4Network(
            '0.0.0.0/' + str(address['mask'])).netmask),
        'hostname': config['name'], 'inform_url': config['controller_url'],
        'time': snapshot['collected_at'], 'uptime': int(snapshot['uptime']),
        'default': not state.get('adopted', False),
        'state': 2 if state.get('adopted') else 0,
        'cfgversion': state.get('cfgversion', ''),
    }
    if not state.get('adopted'):
        return data

    # A lease supplies address metadata; only verified hosts enter host_table.
    hosts = []
    for host in snapshot['live_hosts']:
        entry = {k: host[k] for k in ['mac', 'ip']}
        if host.get('hostname'):
            entry['hostname'] = host['hostname']
        entry.update({'age': 0, 'authorized': True, 'uptime': host.get('observed_for', 0)})
        usage = snapshot['usage'].get(host['mac'], {})
        if usage:
            entry.update({k: usage[k] for k in ['rx_bytes', 'tx_bytes']})
            entry.update(rx_packets=usage['rx_pkts'], tx_packets=usage['tx_pkts'])
        hosts.append(entry)

    memory = snapshot['memory']
    stats = {'mem': str(round(100 * (1 - memory['available'] / memory['total']))),
             'uptime': str(int(snapshot['uptime']))}
    same_boot = previous and previous['boot_id'] == snapshot['boot_id']
    elapsed = snapshot['collected_at'] - previous['collected_at'] if same_boot else 0
    if elapsed > 0:
        cpu_total = snapshot['cpu_total'] - previous['cpu_total']
        if cpu_total > 0:
            stats['cpu'] = str(round(100 * (1 - (
                snapshot['cpu_idle'] - previous['cpu_idle']) / cpu_total)))

    interfaces, networks, routes = [], [], []
    for interface in snapshot['interfaces']:
        name = 'eth0' if interface['role'] == 'wan' else 'eth1'
        address = interface['addresses'][0] if interface['addresses'] else None
        counters = dict(interface['counters'])
        if elapsed > 0:
            old = next(i for i in previous['interfaces'] if i['role'] == interface['role'])
            for direction in ['rx', 'tx']:
                delta = counters[direction + '_bytes'] - old['counters'][direction + '_bytes']
                if delta >= 0:
                    counters[direction + '_bps'] = int(delta / elapsed)
        item = dict(counters, name=name, mac=interface['mac'],
                    up=interface['up'], enable=interface['up'],
                    num_port=1)
        network = {'name': name, 'mac': interface['mac'],
                   'addresses': [],
                   'up': str(interface['up']).lower(), 'stats': counters}
        if address:
            item.update(ip=address['address'], netmask=str(ipaddress.IPv4Network(
                '0.0.0.0/' + str(address['mask'])).netmask))
            network['address'] = address['address'] + '/' + str(address['mask'])
            network['addresses'] = [network['address']]
        if interface['role'] == 'lan':
            network['host_table'] = hosts
        else:
            gateways = [r['nexthop'] for r in interface['routes']
                        if r.get('target') == '0.0.0.0' and r.get('mask') == 0]
            item.update(gateways=gateways, nameservers=interface['dns'])
            network.update(gateways=gateways, nameservers=interface['dns'])
            if snapshot.get('latency_ms') is not None:
                item['latency'] = snapshot['latency_ms']
            for gateway in gateways:
                routes.append({'pfx': '0.0.0.0/0', 'nh': [
                    {'intf': name, 'metric': '1/0', 't': 'S>*', 'via': gateway}]})
        interfaces.append(item)
        networks.append(network)
    data.update({
        'system-stats': stats, 'if_table': interfaces, 'network_table': networks,
        'ipv4_active_leases': [{k: h[k] for k in ['mac', 'ip', 'hostname']}
                               for h in snapshot['leases']],
        'uplink': 'eth0', 'routes': routes, 'has_dpi': False, 'fw_caps': 3,
        'has_eth1': True, 'selfrun_beacon': True, 'discovery_response': False,
        'config_network_wan': {'type': 'dhcp'},
        'config_port_table': [
            {'ifname': 'eth0', 'name': 'WAN', 'type': 'wan'},
            {'ifname': 'eth1', 'name': 'LAN', 'type': 'lan'}],
    })
    return data


class RouterObserver:
    def __init__(self):
        self.previous = None
        self.first_seen = {}
        self.raw_usage = {}
        self.total_usage = {}

    def __call__(self, config, state, started):
        snapshot = read_snapshot(config)
        # nlbwmon starts a new database each accounting period. Keep Inform
        # counters monotonic across that reset, using only observed increments.
        for mac, raw in snapshot['usage'].items():
            if mac not in self.raw_usage:
                self.total_usage[mac] = dict(raw)
            else:
                for key, value in raw.items():
                    delta = value - self.raw_usage[mac][key]
                    self.total_usage[mac][key] += delta if delta >= 0 else value
        self.raw_usage = copy.deepcopy(snapshot['usage'])
        snapshot['usage'] = {mac: dict(self.total_usage[mac]) for mac in snapshot['usage']}
        now = snapshot['collected_at']
        self.first_seen = {h['mac']: self.first_seen.get(h['mac'], now)
                           for h in snapshot['live_hosts']}
        for host in snapshot['live_hosts']:
            host['observed_for'] = now - self.first_seen[host['mac']]
        body = gateway_payload(config, snapshot, state, self.previous)
        self.previous = snapshot
        return body, {'snapshot_at': snapshot['collected_at'],
                      'live_hosts': len(snapshot['live_hosts']),
                      'leases': len(snapshot['leases']),
                      'internet_up': snapshot['internet_up'],
                      'latency_ms': snapshot['latency_ms'],
                      'system_stats': body.get('system-stats'),
                      'usage_periods': snapshot['usage_periods']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='/var/lib/unifi-router-reporter')
    parser.add_argument('--status-dir')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(Path(args.config).read_text())
    if not MAC.fullmatch(config['mac']):
        raise ValueError('Invalid router MAC')
    try:
        run(config, Path(args.state_dir), RouterObserver(),
            Path(args.status_dir) if args.status_dir else None)
    except CompatibilityError as exc:
        logging.error('Router reporter stopped; router configuration unaffected: %s', exc)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
