import copy
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from router_telemetry import RouterObserver, gateway_payload
from openwrt_snapshot import client_candidates, local_usage, verified_hosts


MAC = '02:00:00:00:00:02'
CONFIG = {'mac': '02:00:00:00:00:01', 'name': 'telemetry',
          'controller_url': 'http://192.0.2.2:8080/inform'}


def snapshot():
    return {
        'collected_at': 1000, 'boot_id': 'boot', 'uptime': 100,
        'cpu_total': 1000, 'cpu_idle': 900,
        'memory': {'available': 750, 'total': 1000},
        'interfaces': [
            {'role': 'lan', 'device': 'br-lan', 'mac': CONFIG['mac'],
             'addresses': [{'address': '192.0.2.1', 'mask': 24}],
             'up': True, 'counters': {'rx_bytes': 1000, 'tx_bytes': 2000},
             'routes': [], 'dns': []}],
        'leases': [], 'live_hosts': [{'mac': MAC, 'ip': '192.0.2.10'}],
        'usage': {MAC: {'rx_bytes': 2097152, 'tx_bytes': 20000,
                        'rx_pkts': 1500, 'tx_pkts': 300}},
        'internet_up': True, 'latency_ms': 5, 'usage_periods': [],
    }


def host(payload):
    return payload['network_table'][0]['host_table'][0]


class TrafficDirectionTests(unittest.TestCase):
    def test_download_maps_to_unifi_transmit_and_upload_to_receive(self):
        data = gateway_payload(CONFIG, snapshot(), {'adopted': True})
        client = host(data)
        self.assertEqual(client['tx_bytes'], 2097152)
        self.assertEqual(client['rx_bytes'], 20000)
        self.assertEqual(client['tx_packets'], 1500)
        self.assertEqual(client['rx_packets'], 300)

    def test_startup_baselines_existing_accounting_totals(self):
        observer = RouterObserver()
        with patch('router_telemetry.read_snapshot', return_value=snapshot()):
            data, _ = observer(CONFIG, {'adopted': True}, 0)
        self.assertEqual(host(data)['tx_bytes'], 0)
        self.assertEqual(host(data)['rx_bytes'], 0)

    def test_only_measured_increments_and_resets_are_reported(self):
        observer = RouterObserver()
        source = snapshot()
        with patch('router_telemetry.read_snapshot', side_effect=lambda _: copy.deepcopy(source)):
            observer(CONFIG, {'adopted': True}, 0)
            source['collected_at'] += 30
            source['usage'][MAC]['rx_bytes'] += 2000
            source['usage'][MAC]['tx_bytes'] += 100
            data, _ = observer(CONFIG, {'adopted': True}, 0)
            self.assertEqual(host(data)['tx_bytes'], 2000)
            self.assertEqual(host(data)['rx_bytes'], 100)
            source['collected_at'] += 30
            source['usage'][MAC]['rx_bytes'] = 50
            source['usage'][MAC]['tx_bytes'] = 10
            data, _ = observer(CONFIG, {'adopted': True}, 0)
            self.assertEqual(host(data)['tx_bytes'], 2050)
            self.assertEqual(host(data)['rx_bytes'], 110)

    def test_unaccounted_hosts_keep_counters_absent(self):
        source = snapshot()
        source['usage'] = {}
        client = host(gateway_payload(CONFIG, source, {'adopted': True}))
        self.assertNotIn('tx_bytes', client)
        self.assertNotIn('rx_bytes', client)

    def test_local_counter_attribution_and_direction(self):
        def table(ip, count):
            return {'nftables': [{'set': {'elem': [
                {'elem': {'val': ip, 'counter': {'bytes': count, 'packets': 3}}}]}}]}
        source = snapshot()
        local = local_usage({'rx': table('192.0.2.10', 200),
                             'tx': table('192.0.2.10', 20)}, source['live_hosts'])
        self.assertEqual(local[0]['mac'], MAC)
        self.assertEqual(local[0]['rx_bytes'], 200)
        self.assertEqual(local[0]['tx_bytes'], 20)
        self.assertEqual(local_usage({'rx': table('192.0.2.99', 100)},
                                     source['live_hosts']), [])

    def test_sources_reset_independently_and_reassigned_ip_is_baselined(self):
        observer = RouterObserver()
        source = snapshot()
        source['local_usage'] = [dict(ip='192.0.2.10', mac=MAC, rx_bytes=100,
                                      tx_bytes=50, rx_pkts=3, tx_pkts=2)]
        with patch('router_telemetry.read_snapshot', side_effect=lambda _: copy.deepcopy(source)):
            observer(CONFIG, {'adopted': True}, 0)
            source['usage'][MAC]['rx_bytes'] = 10
            source['local_usage'][0]['rx_bytes'] += 200
            source['collected_at'] += 30
            data, _ = observer(CONFIG, {'adopted': True}, 0)
            self.assertEqual(host(data)['tx_bytes'], 210)
            source['local_usage'][0]['ip'] = '192.0.2.20'
            source['local_usage'][0]['rx_bytes'] = 9999999
            source['collected_at'] += 30
            data, _ = observer(CONFIG, {'adopted': True}, 0)
            self.assertEqual(host(data)['tx_bytes'], 210)

    def test_checkpoint_preserves_counter_continuity_across_publisher_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / 'counters.json'
            source = snapshot()
            with patch('router_telemetry.read_snapshot', side_effect=lambda _: copy.deepcopy(source)):
                RouterObserver(checkpoint)(CONFIG, {'adopted': True}, 0)
                source['usage'][MAC]['rx_bytes'] += 1000
                source['collected_at'] += 30
                data, _ = RouterObserver(checkpoint)(CONFIG, {'adopted': True}, 0)
                self.assertEqual(host(data)['tx_bytes'], 1000)
                source['boot_id'] = 'another-boot'
                data, _ = RouterObserver(checkpoint)(CONFIG, {'adopted': True}, 0)
                self.assertEqual(host(data)['tx_bytes'], 0)

    def test_configured_vlans_use_fresh_arp_and_separate_host_tables(self):
        source = snapshot()
        for device, address in [('eth0.30', '198.51.100.1'), ('eth0.40', '203.0.113.1')]:
            interface = copy.deepcopy(source['interfaces'][0])
            interface.update(device=device, addresses=[{'address': address, 'mask': 24}])
            source['interfaces'].append(interface)
        neighbors = [
            {'ip': '198.51.100.10', 'mac': MAC, 'interface': 'eth0.30', 'state': 'REACHABLE'},
            {'ip': '203.0.113.10', 'mac': '02:00:00:00:00:03',
             'interface': 'eth0.40', 'state': 'STALE'},
        ]
        candidates = client_candidates([], neighbors, source['interfaces'])
        source['live_hosts'] = verified_hosts(candidates, neighbors, set())
        self.assertEqual([h['ip'] for h in source['live_hosts']], ['198.51.100.10'])
        data = gateway_payload(CONFIG, source, {'adopted': True})
        self.assertEqual([n['name'] for n in data['network_table']],
                         ['eth1', 'eth1.30', 'eth1.40'])
        self.assertEqual([len(n['host_table']) for n in data['network_table']], [0, 1, 0])


if __name__ == '__main__':
    unittest.main()
