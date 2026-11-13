import copy
import json
from unittest.mock import patch

import pytest

from collectd_telemetry import format_values, main, sample_rates


MAC = '02:00:00:00:00:01'


def snapshot():
    return {'boot_id': 'boot', 'collected_at': 1000,
            'live_hosts': [{'mac': MAC, 'ip': '192.0.2.10'}],
            'usage': {MAC: {'rx_bytes': 10000, 'tx_bytes': 1000}},
            'local_usage': [{'mac': MAC, 'ip': '192.0.2.10',
                             'rx_bytes': 500, 'tx_bytes': 50}]}


def test_internet_download_and_upload_rates_keep_client_directions():
    old = snapshot()
    current = copy.deepcopy(old)
    current['collected_at'] += 32
    current['usage'][MAC]['rx_bytes'] += 2097152
    current['usage'][MAC]['tx_bytes'] += 3200
    assert sample_rates(current, old)[MAC]['internet'] == (65536, 100)


def test_multiple_local_addresses_are_summed_separately_from_internet():
    old = snapshot()
    old['local_usage'].append(dict(old['local_usage'][0], ip='192.0.2.11'))
    current = copy.deepcopy(old)
    current['collected_at'] += 30
    current['local_usage'][0]['rx_bytes'] += 3000
    current['local_usage'][1]['rx_bytes'] += 6000
    result = sample_rates(current, old)[MAC]
    assert result == {'internet': (0, 0), 'local': (300, 0)}


def test_accounting_resets_do_not_create_negative_rates_or_wraparound():
    old = snapshot()
    current = copy.deepcopy(old)
    current['collected_at'] += 30
    current['usage'][MAC] = {'rx_bytes': 300, 'tx_bytes': 60}
    assert sample_rates(current, old)[MAC]['internet'] == (10, 2)


def test_changed_ip_ownership_does_not_credit_previous_owner_traffic():
    old = snapshot()
    current = copy.deepcopy(old)
    current['collected_at'] += 30
    current['local_usage'][0]['ip'] = '192.0.2.20'
    current['local_usage'][0]['rx_bytes'] = 99999999
    assert sample_rates(current, old)[MAC]['local'] is None


def test_reassigned_address_is_unknown_for_the_new_mac():
    old = snapshot()
    current = copy.deepcopy(old)
    new_mac = '02:00:00:00:00:02'
    current['collected_at'] += 30
    current['live_hosts'][0]['mac'] = new_mac
    current['local_usage'][0].update(mac=new_mac, rx_bytes=99999999)
    assert sample_rates(current, old)[new_mac]['local'] is None


def test_new_boot_first_sample_and_long_gap_are_unknown():
    old = snapshot()
    assert sample_rates(old, None)[MAC] == {'internet': None, 'local': None}
    current = copy.deepcopy(old)
    current['boot_id'] = 'new-boot'
    current['collected_at'] += 30
    assert sample_rates(current, old)[MAC]['internet'] is None
    current['boot_id'] = old['boot_id']
    current['collected_at'] += 100
    assert sample_rates(current, old)[MAC]['internet'] is None


def test_missing_accounting_is_exported_as_unknown_with_stable_mac_identity():
    current = snapshot()
    current['usage'] = {}
    current['local_usage'] = []
    lines = list(format_values(current, snapshot(), 'router', 30))
    assert len(lines) == 4
    assert all('/device_traffic-020000000001/' in line for line in lines)
    assert all(line.endswith(' 1000:U') for line in lines)


def test_reader_skips_startup_and_repeated_timestamps(capsys, monkeypatch):
    old = dict(snapshot(), schema_version=1)
    current = copy.deepcopy(old)
    current['collected_at'] += 30
    current['usage'][MAC]['rx_bytes'] += 3000
    monkeypatch.setenv('COLLECTD_HOSTNAME', 'router')
    monkeypatch.setenv('COLLECTD_INTERVAL', '30')
    with patch('collectd_telemetry.Path.read_text', side_effect=[
            json.dumps(s) for s in [old, old, current, current]]), \
            patch('collectd_telemetry.time.time', return_value=1030), \
            patch('collectd_telemetry.time.sleep', side_effect=[None, None, None, StopIteration]):
        with pytest.raises(StopIteration):
            main()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert all(' 1030:' in line for line in lines)
    assert any(line.endswith(' 1030:100.0') for line in lines)
