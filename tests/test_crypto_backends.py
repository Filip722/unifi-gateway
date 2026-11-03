import json

import pytest

import unifi_protocol as protocol
from client_reporter import codec_config


@pytest.mark.parametrize('gcm', [False, True])
def test_openwrt_crypto_backend_interoperates_with_upstream(monkeypatch, gcm):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    config = codec_config({'mac': '02:00:00:00:00:01'}, {
        'adopted': True, 'key': 'a' * 32, 'gcm': gcm,
    })
    data = {'test': 'cross-backend report'}
    original = protocol.encode_inform(config, json.dumps(data))
    crypto = protocol.AES
    monkeypatch.setattr(protocol, 'AES', None)
    monkeypatch.setattr(protocol, 'Cipher', Cipher, raising=False)
    monkeypatch.setattr(protocol, 'algorithms', algorithms, raising=False)
    monkeypatch.setattr(protocol, 'modes', modes, raising=False)
    assert protocol.decode_inform(config, original) == data
    fallback = protocol.encode_inform(config, json.dumps(data))
    monkeypatch.setattr(protocol, 'AES', crypto)
    assert protocol.decode_inform(config, fallback) == data
