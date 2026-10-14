import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.security.identity import DeviceIdentity
from lan_input_share.security.pairing import PairingCoordinator, PairingRequired
from lan_input_share.security.trust import TrustStore


def _identity(root, name):
    paths = AppPaths(root, root / "config.json", root / "logs", root / "identity", root / "staging")
    return DeviceIdentity.load_or_create(paths, name)


def test_signed_challenge_rejects_signature_from_other_identity(tmp_path):
    controller = _identity(tmp_path / "controller", "controller")
    expected_client = _identity(tmp_path / "expected", "expected")
    attacker = _identity(tmp_path / "attacker", "attacker")
    coordinator = PairingCoordinator(controller, TrustStore(tmp_path / "trust.json"))
    request = coordinator.begin(expected_client.hello())
    coordinator.approve(request.request_id)

    with pytest.raises(ValueError, match="签名验证失败"):
        coordinator.authenticate(
            b"nonce", attacker.sign(b"nonce"), expected_client.certificate_der
        )


@pytest.mark.asyncio
async def test_first_pairing_requires_approval_on_both_peers(tmp_path):
    controller = _identity(tmp_path / "controller", "controller")
    client = _identity(tmp_path / "client", "client")
    controller_pairing = PairingCoordinator(
        controller, TrustStore(tmp_path / "controller-trust.json")
    )
    client_pairing = PairingCoordinator(client, TrustStore(tmp_path / "client-trust.json"))
    controller_request = controller_pairing.begin(client.hello())
    client_request = client_pairing.begin(controller.hello())

    with pytest.raises(PairingRequired):
        controller_pairing.authenticate(
            b"controller-nonce", client.sign(b"controller-nonce"), client.certificate_der
        )

    controller_pairing.approve(controller_request.request_id)
    client_pairing.approve(client_request.request_id)

    authenticated_client = controller_pairing.authenticate(
        b"controller-nonce", client.sign(b"controller-nonce"), client.certificate_der
    )
    authenticated_controller = client_pairing.authenticate(
        b"client-nonce", controller.sign(b"client-nonce"), controller.certificate_der
    )
    assert authenticated_client.device_id == client.device_id
    assert authenticated_controller.device_id == controller.device_id


@pytest.mark.asyncio
async def test_pairing_decision_binds_first_controller_only_after_approval(tmp_path):
    client = _identity(tmp_path / "client", "client")
    controller = _identity(tmp_path / "controller", "controller")
    store = TrustStore(tmp_path / "trust.json")
    coordinator = PairingCoordinator(client, store)
    seen = []

    async def approve(request):
        seen.append(request)
        return True

    assert await coordinator.decide_controller(controller.hello(), approve, timeout=1)
    assert seen[0].peer.device_name == "controller"
    assert store.bound_controller() is not None
    assert store.bound_controller().device_id == controller.device_id


@pytest.mark.asyncio
async def test_pairing_remembers_multiple_approved_controllers(tmp_path):
    client = _identity(tmp_path / "client-many", "client")
    first = _identity(tmp_path / "controller-first", "first")
    second = _identity(tmp_path / "controller-second", "second")
    store = TrustStore(tmp_path / "trust-many.json")
    coordinator = PairingCoordinator(client, store)

    async def approve(_request):
        return True

    assert await coordinator.decide_controller(first.hello(), approve, timeout=1)
    assert await coordinator.decide_controller(second.hello(), approve, timeout=1)

    assert store.verify(first.device_id, first.certificate_der)
    assert store.verify(second.device_id, second.certificate_der)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reject", "error", "timeout"])
async def test_rejected_closed_or_timed_out_pairing_does_not_write_trust(
    tmp_path, mode
):
    client = _identity(tmp_path / f"client-{mode}", "client")
    controller = _identity(tmp_path / f"controller-{mode}", "controller")
    store = TrustStore(tmp_path / f"trust-{mode}.json")
    coordinator = PairingCoordinator(client, store)

    async def decide(_request):
        if mode == "error":
            raise RuntimeError("dialog closed")
        if mode == "timeout":
            await __import__("asyncio").sleep(1)
        return False

    assert not await coordinator.decide_controller(
        controller.hello(), decide, timeout=0.01
    )
    assert store.bound_controller() is None
    assert store.trusted_devices() == ()
