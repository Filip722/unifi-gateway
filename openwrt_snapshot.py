"""Fixed read-only OpenWrt snapshot command; accepts no remote arguments."""
import concurrent.futures
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time


def command(*args):
    return subprocess.check_output(args, timeout=8, text=True)


def parse_leases(text, now):
    leases = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        expiry, mac, ip, name = fields[:4]
        if (int(expiry) == 0 or int(expiry) > now) and ':' not in ip:
            leases.append({'mac': mac.lower(), 'ip': str(ipaddress.IPv4Address(ip)),
                           'hostname': name if name != '*' else '',
                           'expires': int(expiry)})
    return leases


def parse_neighbors(text):
    neighbors = []
    for line in text.splitlines():
        fields = line.split()
        if 'lladdr' not in fields or 'dev' not in fields:
            continue
        ip = str(ipaddress.IPv4Address(fields[0]))
        neighbors.append({'ip': ip, 'mac': fields[fields.index('lladdr') + 1].lower(),
                          'interface': fields[fields.index('dev') + 1],
                          'state': fields[-1]})
    return neighbors


def aggregate_usage(table):
    totals = {}
    for row in table['data']:
        item = dict(zip(table['columns'], row))
        mac = item['mac'].lower()
        if mac == '00:00:00:00:00:00':
            continue
        host = totals.setdefault(mac, dict.fromkeys(
            ['rx_bytes', 'tx_bytes', 'rx_pkts', 'tx_pkts', 'conns'], 0))
        for key in host:
            host[key] += int(item.get(key, 0))
    return totals


def collect():
    config = json.loads(Path('/etc/unifi-telemetry-snapshot.json').read_text())
    source = json.loads(command('ubus', 'call', 'network.interface', 'dump'))['interface']
    interfaces = []
    for role, logical in [('lan', config['lan_interface']), ('wan', config['wan_interface'])]:
        item = next(i for i in source if i['interface'] == logical)
        dev = item.get('l3_device', item.get('device', ''))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', dev):
            raise ValueError('Invalid interface name')
        sysfs = Path('/sys/class/net') / dev
        counters = {name: int((sysfs / 'statistics' / name).read_text())
                    for name in ['rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                                 'rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped']}
        interfaces.append({
            'role': role, 'device': dev, 'up': item['up'],
            'addresses': item.get('ipv4-address', []),
            'mac': (sysfs / 'address').read_text().strip(), 'counters': counters,
            'routes': item.get('route', []), 'dns': item.get('dns-server', []),
        })

    now = int(time.time())
    leases = parse_leases(Path('/tmp/dhcp.leases').read_text(), now)
    neighbors = parse_neighbors(command('ip', '-4', 'neigh', 'show'))
    local_networks = [ipaddress.ip_network(a['address'] + '/' + str(a['mask']), strict=False)
                      for i in source if i['interface'] == config['lan_interface']
                      for a in i.get('ipv4-address', [])]
    candidates = {x['ip']: x for x in neighbors + leases
                  if any(ipaddress.ip_address(x['ip']) in net for net in local_networks)}

    def ping(ip):
        result = subprocess.run(['ping', '-n', '-q', '-c', '1', '-W', '1', ip],
                                capture_output=True, timeout=3, check=False)
        return ip if result.returncode == 0 else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responding = set(pool.map(ping, candidates)) - {None}
    fresh = parse_neighbors(command('ip', '-4', 'neigh', 'show'))
    fresh = {n['ip']: n for n in fresh if n['state'] not in ['FAILED', 'INCOMPLETE']}
    live = [dict(candidates[ip], mac=fresh[ip]['mac']) for ip in responding
            if ip in fresh and fresh[ip]['mac'] == candidates[ip]['mac']]

    system = json.loads(command('ubus', 'call', 'system', 'info'))
    cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    usage = aggregate_usage(json.loads(command('nlbw', '-c', 'json')))
    health = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', '1.1.1.1'],
                            capture_output=True, text=True, timeout=3, check=False)
    latency = re.search(r'time[=<]([0-9.]+)', health.stdout)
    return {
        'schema_version': 1, 'collected_at': int(time.time()),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'hostname': Path('/proc/sys/kernel/hostname').read_text().strip(),
        'uptime': system['uptime'], 'memory': system['memory'], 'load': system['load'],
        'cpu_total': sum(cpu[:8]), 'cpu_idle': cpu[3] + cpu[4],
        'interfaces': interfaces, 'leases': leases, 'live_hosts': live,
        'usage': usage, 'usage_periods': command('nlbw', '-c', 'list').splitlines(),
        'internet_up': health.returncode == 0,
        'latency_ms': float(latency[1]) if latency else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--loop', action='store_true')
    args = parser.parse_args()
    while True:
        try:
            data = json.dumps(collect())
            if args.output:
                path = Path(args.output)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(data)
                temporary.chmod(0o640)
                temporary.replace(path)
            else:
                print(data)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            if not args.loop:
                raise
            print('Snapshot failed: ' + type(exc).__name__, flush=True)
        if not args.loop:
            break
        time.sleep(30)


if __name__ == '__main__':
    main()
