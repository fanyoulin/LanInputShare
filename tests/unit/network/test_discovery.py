import json

from lan_input_share.network.discovery import (
    DiscoveryAnnouncement,
    DiscoveryRegistry,
)


def test_discovery_announcement_contains_no_private_key_or_auth_token():
    announcement = DiscoveryAnnouncement(
        device_id="peer", device_name="desk", role="client", port=24870, fingerprint="abc"
    )

    wire = announcement.to_bytes()

    assert json.loads(wire) == {
        "device_id": "peer",
        "device_name": "desk",
        "fingerprint": "abc",
        "port": 24870,
        "role": "client",
        "version": 1,
    }
    assert b"private" not in wire.lower()
    assert b"token" not in wire.lower()


def test_discovery_deduplicates_same_device_seen_on_two_addresses():
    registry = DiscoveryRegistry()
    message = DiscoveryAnnouncement("peer", "desk", "client", 24870, "abc")

    registry.update(message, "192.168.1.10", "wifi")
    registry.update(message, "192.168.1.11", "ethernet")

    peers = registry.peers()
    assert len(peers) == 1
    assert peers[0].device_id == "peer"
    assert peers[0].addresses == ("192.168.1.10", "192.168.1.11")


def test_discovery_rejects_wrong_version_and_invalid_role():
    assert DiscoveryAnnouncement.from_bytes(b'{"version":2}') is None
    payload = (
        b'{"version":1,"device_id":"p","device_name":"d","role":"server",'
        b'"port":1,"fingerprint":"f"}'
    )
    assert DiscoveryAnnouncement.from_bytes(payload) is None
