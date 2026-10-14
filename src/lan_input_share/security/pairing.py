from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from lan_input_share.security.identity import (
    DeviceIdentity,
    PeerHello,
    certificate_fingerprint,
    short_fingerprint,
    verify_signature,
)
from lan_input_share.security.trust import TrustStore


class PairingRequired(PermissionError):
    """Raised when a peer has not been approved locally."""


@dataclass(frozen=True, slots=True)
class PairingRequest:
    request_id: str
    peer: PeerHello
    short_fingerprint: str


@dataclass(frozen=True, slots=True)
class AuthenticatedPeer:
    device_id: str
    fingerprint: str


type PairingDecisionCallback = Callable[[PairingRequest], Awaitable[bool]]


class PairingCoordinator:
    def __init__(self, identity: DeviceIdentity, trust_store: TrustStore) -> None:
        self._identity = identity
        self._trust_store = trust_store
        self._pending: dict[str, PairingRequest] = {}

    def begin(self, peer: PeerHello) -> PairingRequest:
        request = PairingRequest(
            request_id=str(uuid.uuid4()),
            peer=peer,
            short_fingerprint=short_fingerprint(peer.certificate_der),
        )
        self._pending[request.request_id] = request
        return request

    def approve(self, request_id: str) -> None:
        try:
            request = self._pending.pop(request_id)
        except KeyError as exc:
            raise PairingRequired("配对请求不存在或已过期") from exc
        self._trust_store.approve(request.peer.device_id, request.peer.certificate_der)

    async def decide_controller(
        self,
        peer: PeerHello,
        decider: PairingDecisionCallback,
        *,
        timeout: float = 30.0,
    ) -> bool:
        request = self.begin(peer)
        try:
            approved = await asyncio.wait_for(decider(request), timeout=timeout)
        except Exception:
            approved = False
        self._pending.pop(request.request_id, None)
        if not approved:
            return False
        self._trust_store.bind_controller(peer.device_id, peer.certificate_der)
        return True

    def authenticate(
        self,
        challenge: bytes,
        signature: bytes,
        certificate_der: bytes,
    ) -> AuthenticatedPeer:
        trusted = self._trust_store.find_by_certificate(certificate_der)
        if trusted is None:
            raise PairingRequired("设备尚未在本机批准")
        verify_signature(certificate_der, challenge, signature)
        return AuthenticatedPeer(trusted.device_id, certificate_fingerprint(certificate_der))
