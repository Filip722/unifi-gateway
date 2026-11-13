import copy
import json
import time
from unittest.mock import patch

import pytest

from client_reporter import CompatibilityError
from openwrt_snapshot import aggregate_usage, parse_leases, parse_neighbors
from router_telemetry import RouterObserver, gateway_payload, read_snapshot


@pytest.fixture
def snapshot():
    counters = dict(rx_bytes=1000, tx_bytes=2000, rx_packets=10, tx_packets=20,
                    rx_errors=0, tx_errors=0, rx_dropped=0, tx_dropped=0)
    return {
        'schema_version': 1, 'collected_at': int(time.time()), 'boot_id': 'boot-1',
        'uptime': 100, 'cpu_total': 1000, 'cpu_idle': 900,
        'memory': {'available': 750, 'total': 1000},
        'interfaces': [
            {'role': 'lan', 'device': 'br-lan', 'addresses': [{'address': '192.0.2.1', 'mask': 24}],
             'mac': '00:11:22:33:44:55', 'up': True, 'counters': counters,
             'routes': [], 'dns': []},
            {'role': 'wan', 'device': 'eth1', 'addresses': [{'address': '198.51.100.2', 'mask': 24}],
             'mac': '00:11:22:33:44:56', 'up': True, 'counters': counters,
             'routes': [{'target': '0.0.0.0', 'mask': 0, 'nexthop': '198.51.100.1'}],
             'dns': ['198.51.100.1']},
        ],
        'leases': [{'mac': '00:11:22:33:44:01', 'ip': '192.0.2.10', 'hostname': 'live'},
                   {'mac': '00:11:22:33:44:02', 'ip': '192.0.2.20', 'hostname': 'asleep'}],
        'live_hosts': [{'mac': '00:11:22:33:44:01', 'ip': '192.0.2.10', 'hostname': 'live'}],
        'usage': {'00:11:22:33:44:01': {'rx_bytes': 123, 'tx_bytes': 456,
                                       'rx_pkts': 12, 'tx_pkts': 45}},
        'internet_up': True, 'latency_ms': 5.1, 'usage_periods': ['2026-11-01'],
    }


@pytest.fixture
def config():
    return json.load(open('conf/router-telemetry.sample.json'))


def test_expired_lease_is_not_address_metadata():
    text = ('99 00:11:22:33:44:01 192.0.2.1 old *\n'
            '101 00:11:22:33:44:02 192.0.2.2 live *\n'
            '0 00:11:22:33:44:03 192.0.2.3 static *\n')
    assert [h['hostname'] for h in parse_leases(text, 100)] == ['live', 'static']


def test_failed_neighbor_is_not_a_host():
    text = ('192.0.2.1 dev br-lan lladdr 00:11:22:33:44:01 REACHABLE\n'
            '192.0.2.2 dev br-lan FAILED\n')
    assert len(parse_neighbors(text)) == 1


def test_traffic_aggregates_protocols_and_ip_families_without_fake_values():
    columns = ['mac', 'rx_bytes', 'tx_bytes', 'rx_pkts', 'tx_pkts', 'conns']
    table = {'columns': columns, 'data': [
        ['00:11:22:33:44:01', 100, 200, 1, 2, 3],
        ['00:11:22:33:44:01', 400, 500, 4, 5, 6],
    ]}
    assert aggregate_usage(table)['00:11:22:33:44:01'] == {
        'rx_bytes': 500, 'tx_bytes': 700, 'rx_pkts': 5, 'tx_pkts': 7, 'conns': 9,
    }


def test_leases_do_not_become_live_hosts(config, snapshot):
    data = gateway_payload(config, snapshot, {'adopted': True})
    lan = next(n for n in data['network_table'] if n['name'] == 'eth1')
    assert len(data['ipv4_active_leases']) == 2
    assert [h['hostname'] for h in lan['host_table']] == ['live']
    assert lan['host_table'][0]['tx_bytes'] == 123
    assert data['system-stats']['mem'] == '25'
    assert 'cpu' not in data['system-stats']


def test_rates_use_deltas_and_reset_on_router_reboot(config, snapshot):
    previous = copy.deepcopy(snapshot)
    previous['collected_at'] -= 10
    previous['cpu_total'] -= 100
    previous['cpu_idle'] -= 80
    for i in previous['interfaces']:
        i['counters']['rx_bytes'] = 500
    data = gateway_payload(config, snapshot, {'adopted': True}, previous)
    assert data['if_table'][0]['rx_bps'] == 50
    assert data['system-stats']['cpu'] == '20'
    previous['boot_id'] = 'old-boot'
    data = gateway_payload(config, snapshot, {'adopted': True}, previous)
    assert 'rx_bps' not in data['if_table'][0]


def test_wan_dhcp_loss_does_not_stop_lan_reporting(config, snapshot):
    snapshot['interfaces'][1]['up'] = False
    snapshot['interfaces'][1]['addresses'] = []
    snapshot['internet_up'] = False
    snapshot['latency_ms'] = None
    data = gateway_payload(config, snapshot, {'adopted': True})
    wan = next(i for i in data['if_table'] if i['name'] == 'eth0')
    assert wan['up'] is False
    assert 'ip' not in wan
    assert data['network_table'][0]['host_table'][0]['ip'] == '192.0.2.10'


def test_accounting_period_reset_does_not_make_negative_traffic(config, snapshot):
    observer = RouterObserver()
    with patch('router_telemetry.read_snapshot', return_value=copy.deepcopy(snapshot)):
        observer(config, {'adopted': True}, 0)
    snapshot['usage']['00:11:22:33:44:01']['rx_bytes'] = 5
    snapshot['collected_at'] += 30
    with patch('router_telemetry.read_snapshot', return_value=snapshot):
        data, _ = observer(config, {'adopted': True}, 0)
    host = data['network_table'][0]['host_table'][0]
    assert host['tx_bytes'] == 5


@pytest.mark.parametrize('change', ['old', 'schema', 'identity'])
def test_invalid_snapshot_is_not_published(config, snapshot, change):
    if change == 'old':
        snapshot['collected_at'] -= 120
    elif change == 'schema':
        snapshot['schema_version'] = 999
    else:
        snapshot['interfaces'][0]['mac'] = '00:00:00:00:00:01'
    with patch('router_telemetry.Path.read_text', return_value=json.dumps(snapshot)):
        with pytest.raises((ValueError, CompatibilityError)):
            read_snapshot(config)
