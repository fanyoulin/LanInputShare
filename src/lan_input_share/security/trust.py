from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from lan_input_share.security.identity import certificate_fingerprint


class TrustError(ValueError):
    """Raised when a peer conflicts with an existing trust decision."""


@dataclass(frozen=True, slots=True, order=True)
class TrustedDevice:
    device_id: str
    fingerprint: str
    certificate_der_b64: str

    @property
    def certificate_der(self) -> bytes:
        return base64.b64decode(self.certificate_der_b64, validate=True)


@dataclass(frozen=True, slots=True, order=True)
class TrustedEndpoint:
    endpoint_key: str
    device_id: str
    fingerprint: str


class TrustStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._devices, self._bound_controller_id, self._endpoints = self._load()

    def approve(self, device_id: str, certificate_der: bytes) -> TrustedDevice:
        fingerprint = certificate_fingerprint(certificate_der)
        existing = self._devices.get(device_id)
        if existing is not None and existing.fingerprint != fingerprint:
            raise TrustError(f"设备 {device_id} 的证书指纹不匹配")
        trusted = TrustedDevice(
            device_id=device_id,
            fingerprint=fingerprint,
            certificate_der_b64=base64.b64encode(certificate_der).decode("ascii"),
        )
        self._devices[device_id] = trusted
        self._save()
        return trusted

    def verify(self, device_id: str, certificate_der: bytes) -> bool:
        trusted = self._devices.get(device_id)
        return trusted is not None and trusted.fingerprint == certificate_fingerprint(
            certificate_der
        )

    def find_by_certificate(self, certificate_der: bytes) -> TrustedDevice | None:
        fingerprint = certificate_fingerprint(certificate_der)
        return next(
            (device for device in self._devices.values() if device.fingerprint == fingerprint), None
        )

    def bind_controller(self, device_id: str, certificate_der: bytes) -> TrustedDevice:
        trusted = self.approve(device_id, certificate_der)
        self._bound_controller_id = device_id
        self._save()
        return trusted

    def bound_controller(self) -> TrustedDevice | None:
        if self._bound_controller_id is None:
            return None
        return self._devices.get(self._bound_controller_id)

    def revoke_bound_controller(self) -> None:
        device_id = self._bound_controller_id
        if device_id is None:
            return
        self._bound_controller_id = None
        self._devices.pop(device_id, None)
        self._save()

    def approve_endpoint(
        self, endpoint_key: str, device_id: str, certificate_der: bytes
    ) -> TrustedEndpoint:
        fingerprint = certificate_fingerprint(certificate_der)
        existing = self._endpoints.get(endpoint_key)
        if existing is not None and (
            existing.device_id != device_id or existing.fingerprint != fingerprint
        ):
            raise TrustError("目标地址的设备身份不匹配")
        self.approve(device_id, certificate_der)
        endpoint = TrustedEndpoint(endpoint_key, device_id, fingerprint)
        self._endpoints[endpoint_key] = endpoint
        self._save()
        return endpoint

    def verify_endpoint(
        self, endpoint_key: str, device_id: str, certificate_der: bytes
    ) -> bool:
        endpoint = self._endpoints.get(endpoint_key)
        return endpoint is not None and endpoint == TrustedEndpoint(
            endpoint_key, device_id, certificate_fingerprint(certificate_der)
        )

    def endpoint_binding(self, endpoint_key: str) -> TrustedEndpoint | None:
        return self._endpoints.get(endpoint_key)

    def revoke(self, device_id: str) -> None:
        if self._devices.pop(device_id, None) is not None:
            self._save()

    def trusted_devices(self) -> tuple[TrustedDevice, ...]:
        return tuple(sorted(self._devices.values()))

    def _load(
        self,
    ) -> tuple[dict[str, TrustedDevice], str | None, dict[str, TrustedEndpoint]]:
        if not self._path.exists():
            return {}, None, {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        devices = (TrustedDevice(**entry) for entry in payload.get("devices", []))
        endpoints = (
            TrustedEndpoint(**entry) for entry in payload.get("endpoints", [])
        )
        raw_bound_controller_id = payload.get("bound_controller_id")
        bound_controller_id = (
            raw_bound_controller_id
            if isinstance(raw_bound_controller_id, str)
            else None
        )
        return (
            {device.device_id: device for device in devices},
            bound_controller_id,
            {endpoint.endpoint_key: endpoint for endpoint in endpoints},
        )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        payload = {
            "version": 2,
            "devices": [asdict(item) for item in self.trusted_devices()],
            "bound_controller_id": self._bound_controller_id,
            "endpoints": [
                asdict(item) for item in sorted(self._endpoints.values())
            ],
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
