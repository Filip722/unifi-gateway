"""Feed per-device rates from existing OpenWrt snapshots to collectd Exec."""
import json
import os
from pathlib import Path
import sys
import time


def difference(current, previous, elapsed):
    return tuple((value - old if value >= old else value) / elapsed
                 for value, old in zip(current, previous))


def sample_rates(current, previous):
    devices = {h['mac'] for h in current['live_hosts']} | set(current['usage'])
    rates = {mac: {'internet': None, 'local': None} for mac in devices}
    elapsed = current['collected_at'] - previous['collected_at'] if previous else 0
    if not previous or current['boot_id'] != previous['boot_id'] or not 0 < elapsed <= 90:
        return rates
    old_local = {(h['mac'], h['ip']): h for h in previous.get('local_usage', [])}
    for mac in devices:
        if mac in current['usage'] and mac in previous['usage']:
            rates[mac]['internet'] = difference(
                [current['usage'][mac][k] for k in ['rx_bytes', 'tx_bytes']],
                [previous['usage'][mac][k] for k in ['rx_bytes', 'tx_bytes']], elapsed)
        local = [h for h in current.get('local_usage', []) if h['mac'] == mac]
        if local and all((mac, h['ip']) in old_local for h in local):
            deltas = [difference(
                [h[k] for k in ['rx_bytes', 'tx_bytes']],
                [old_local[mac, h['ip']][k] for k in ['rx_bytes', 'tx_bytes']], elapsed)
                for h in local]
            rates[mac]['local'] = tuple(map(sum, zip(*deltas)))
    return rates


def format_values(snapshot, previous, hostname, interval):
    for mac, sources in sorted(sample_rates(snapshot, previous).items()):
        instance = mac.replace(':', '')
        for source, pair in sources.items():
            for index, direction in enumerate(['download', 'upload']):
                value = 'U' if pair is None else str(pair[index])
                yield (f'PUTVAL "{hostname}/device_traffic-{instance}/bytes-{source}_{direction}" '
                       f'interval={interval} {snapshot["collected_at"]}:{value}')


def main():
    previous = None
    hostname = os.environ['COLLECTD_HOSTNAME']
    interval = os.environ.get('COLLECTD_INTERVAL', '30')
    while True:
        try:
            snapshot = json.loads(Path('/tmp/unifi-telemetry/snapshot.json').read_text())
            if snapshot['schema_version'] != 1 or not -5 <= time.time() - snapshot['collected_at'] <= 60:
                raise ValueError('Snapshot is not fresh')
            # On restart the newest snapshot may already be stored in the RRDs.
            # Wait for a fresh pair rather than replaying that timestamp.
            if previous and snapshot['collected_at'] != previous['collected_at']:
                for line in format_values(snapshot, previous, hostname, interval):
                    print(line)
                sys.stdout.flush()
            previous = snapshot
        except (OSError, ValueError, KeyError) as exc:
            print('Telemetry graph sample unavailable: ' + type(exc).__name__, file=sys.stderr)
        time.sleep(5)


if __name__ == '__main__':
    main()
