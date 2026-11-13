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


def local_usage(tables, hosts):
    by_ip = {h['ip']: h['mac'] for h in hosts}
    usage = {}
    for direction, table in tables.items():
        for item in table['nftables']:
            for element in item.get('set', {}).get('elem', []):
                entry = element['elem']
                ip = entry['val']
                if ip not in by_ip:
                    continue
                host = usage.setdefault(ip, dict(ip=ip, mac=by_ip[ip], **dict.fromkeys(
                    ['rx_bytes', 'tx_bytes', 'rx_pkts', 'tx_pkts'], 0)))
                host[direction + '_bytes'] = entry['counter']['bytes']
                host[direction + '_pkts'] = entry['counter']['packets']
    return list(usage.values())


def client_candidates(leases, neighbors, interfaces):
    networks = [(i['device'], ipaddress.ip_network(
        a['address'] + '/' + str(a['mask']), strict=False))
        for i in interfaces if i['role'] == 'lan' and i['up']
        for a in i['addresses']]
    candidates = {}
    for host in neighbors + leases:
        for device, network in networks:
            if ipaddress.ip_address(host['ip']) in network:
                candidates[host['ip']] = dict(host, interface=device)
                break
    return candidates


def verified_hosts(candidates, neighbors, responding):
    fresh = {(n['ip'], n['interface']): n for n in neighbors
             if n['state'] not in ('FAILED', 'INCOMPLETE')}
    live = []
    for ip, host in candidates.items():
        neighbor = fresh.get((ip, host['interface']))
        if not neighbor or neighbor['mac'] != host['mac']:
            continue
        # ARP reachability also proves presence when the host blocks ICMP.
        if ip in responding or neighbor['state'] == 'REACHABLE':
            live.append(host)
    return live


def collect():
    config = json.loads(Path('/etc/unifi-telemetry-snapshot.json').read_text())
    source = json.loads(command('ubus', 'call', 'network.interface', 'dump'))['interface']
    interfaces = []
    wan_interface = config['wan_interface']
    for logical in config.get('wan_interfaces', [config['wan_interface']]):
        item = next((i for i in source if i['interface'] == logical), None)
        if item and item['up']:
            wan_interface = logical
            break
    lan_interfaces = config.get('lan_interfaces', [config['lan_interface']])
    for role, logical in [('lan', name) for name in lan_interfaces] + [('wan', wan_interface)]:
        item = next(i for i in source if i['interface'] == logical)
        dev = item.get('l3_device', item.get('device', ''))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', dev):
            raise ValueError('Invalid interface name')
        sysfs = Path('/sys/class/net') / dev
        missing_wan = role == 'wan' and not sysfs.exists()
        counters = {name: 0 if missing_wan else int((sysfs / 'statistics' / name).read_text())
                    for name in ['rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                                 'rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped']}
        interfaces.append({
            'role': role, 'logical': logical, 'device': dev,
            'up': item['up'] and not missing_wan,
            'addresses': item.get('ipv4-address', []),
            'mac': '00:00:00:00:00:00' if missing_wan else (sysfs / 'address').read_text().strip(),
            'counters': counters,
            'routes': item.get('route', []), 'dns': item.get('dns-server', []),
        })

    now = int(time.time())
    leases = parse_leases(Path('/tmp/dhcp.leases').read_text(), now)
    neighbors = parse_neighbors(command('ip', '-4', 'neigh', 'show'))
    candidates = client_candidates(leases, neighbors, interfaces)

    def ping(ip):
        result = subprocess.run(['ping', '-n', '-q', '-c', '1', '-W', '1', ip],
                                capture_output=True, timeout=3, check=False)
        return ip if result.returncode == 0 else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responding = set(pool.map(ping, candidates)) - {None}
    fresh = parse_neighbors(command('ip', '-4', 'neigh', 'show'))
    live = verified_hosts(candidates, fresh, responding)

    system = json.loads(command('ubus', 'call', 'system', 'info'))
    cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    usage = aggregate_usage(json.loads(command('nlbw', '-c', 'json')))
    local = local_usage({direction: json.loads(command(
        'nft', '-j', 'list', 'set', 'inet', 'fw4', 'unifi_local_' + direction))
        for direction in ['rx', 'tx']}, live) if config.get('local_traffic') else []
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
        'usage': usage, 'local_usage': local,
        'usage_periods': command('nlbw', '-c', 'list').splitlines(),
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
