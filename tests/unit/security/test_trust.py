import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.security.identity import DeviceIdentity
from lan_input_share.security.trust import TrustError, TrustStore


def _identity(root, name):
    paths = AppPaths(root, root / "config.json", root / "logs", root / "identity", root / "staging")
    return DeviceIdentity.load_or_create(paths, name)


def test_trust_store_rejects_same_device_id_with_new_fingerprint(tmp_path):
    first = _identity(tmp_path / "one", "one")
    second = _identity(tmp_path / "two", "two")
    store = TrustStore(tmp_path / "trust.json")
    store.approve("peer", first.certificate_der)

    with pytest.raises(TrustError, match="证书指纹不匹配"):
        store.approve("peer", second.certificate_der)


def test_revoke_requires_pairing_again(tmp_path):
    identity = _identity(tmp_path / "peer", "peer")
    store = TrustStore(tmp_path / "trust.json")
    store.approve("peer", identity.certificate_der)
    assert store.verify("peer", identity.certificate_der)

    store.revoke("peer")

    assert not store.verify("peer", identity.certificate_der)
    assert TrustStore(tmp_path / "trust.json").trusted_devices() == ()


def test_trust_store_persists_certificate_pin_atomically(tmp_path):
    identity = _identity(tmp_path / "peer", "peer")
    path = tmp_path / "trust.json"

    approved = TrustStore(path).approve("peer", identity.certificate_der)

    reloaded = TrustStore(path)
    assert reloaded.verify("peer", identity.certificate_der)
    assert reloaded.trusted_devices() == (approved,)
    assert list(tmp_path.glob("*.tmp")) == []


def test_client_remembers_multiple_approved_controllers(tmp_path):
    first = _identity(tmp_path / "one", "one")
    second = _identity(tmp_path / "two", "two")
    store = TrustStore(tmp_path / "trust.json")

    first_bound = store.bind_controller(first.device_id, first.certificate_der)
    second_bound = store.bind_controller(second.device_id, second.certificate_der)

    assert store.bound_controller() == second_bound
    assert store.verify(first.device_id, first.certificate_der)
    assert store.verify(second.device_id, second.certificate_der)
    assert first_bound in store.trusted_devices()

    store.revoke_bound_controller()
    assert store.bound_controller() is None
    assert store.verify(first.device_id, first.certificate_der)
    assert not store.verify(second.device_id, second.certificate_der)


def test_configured_endpoint_rejects_replacement_identity(tmp_path):
    first = _identity(tmp_path / "one", "one")
    second = _identity(tmp_path / "two", "two")
    store = TrustStore(tmp_path / "trust.json")
    endpoint_key = "192.168.110.157:24870"
    store.approve_endpoint(endpoint_key, first.device_id, first.certificate_der)

    assert store.verify_endpoint(endpoint_key, first.device_id, first.certificate_der)
    with pytest.raises(TrustError, match="目标地址的设备身份不匹配"):
        store.approve_endpoint(endpoint_key, second.device_id, second.certificate_der)
    assert not store.verify_endpoint(
        endpoint_key, second.device_id, second.certificate_der
    )
