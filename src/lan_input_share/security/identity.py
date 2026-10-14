from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from lan_input_share.config.paths import AppPaths


@dataclass(frozen=True, slots=True)
class PeerHello:
    device_id: str
    device_name: str
    certificate_der: bytes


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    device_name: str
    certificate_der: bytes
    certificate_path: Path
    private_key_path: Path
    _private_key: ec.EllipticCurvePrivateKey

    @property
    def fingerprint(self) -> str:
        return certificate_fingerprint(self.certificate_der)

    def sign(self, challenge: bytes) -> bytes:
        return self._private_key.sign(challenge, ec.ECDSA(hashes.SHA256()))

    def hello(self) -> PeerHello:
        return PeerHello(self.device_id, self.device_name, self.certificate_der)

    @classmethod
    def load_or_create(cls, paths: AppPaths, device_name: str) -> DeviceIdentity:
        paths.identity_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = paths.identity_dir / "device.json"
        key_path = paths.identity_dir / "identity-key.pem"
        cert_path = paths.identity_dir / "identity-cert.pem"
        if metadata_path.exists() and key_path.exists() and cert_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise ValueError("身份私钥类型无效")
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            return cls(
                device_id=str(metadata["device_id"]),
                device_name=str(metadata["device_name"]),
                certificate_der=cert.public_bytes(serialization.Encoding.DER),
                certificate_path=cert_path,
                private_key_path=key_path,
                _private_key=key,
            )

        identity = _create_identity(device_name, key_path, cert_path)
        _atomic_write_json(
            metadata_path,
            {"device_id": identity.device_id, "device_name": identity.device_name},
        )
        return identity


def _create_identity(device_name: str, key_path: Path, cert_path: Path) -> DeviceIdentity:
    if not device_name.strip():
        raise ValueError("设备名称不能为空")
    device_id = str(uuid.uuid4())
    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LanInputShare"),
            x509.NameAttribute(NameOID.COMMON_NAME, device_name),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, device_id),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return DeviceIdentity(
        device_id=device_id,
        device_name=device_name,
        certificate_der=certificate.public_bytes(serialization.Encoding.DER),
        certificate_path=cert_path,
        private_key_path=key_path,
        _private_key=private_key,
    )


def certificate_fingerprint(certificate_der: bytes) -> str:
    return hashlib.sha256(certificate_der).hexdigest()


def short_fingerprint(certificate_der: bytes) -> str:
    digest = certificate_fingerprint(certificate_der).upper()
    return " ".join(digest[index : index + 4] for index in range(0, 32, 4))


def authentication_proof_payload(
    channel_binding: bytes, challenge: bytes
) -> bytes:
    if not channel_binding:
        raise ValueError("TLS 通道绑定不能为空")
    if len(challenge) != 32:
        raise ValueError("认证挑战长度无效")
    return b"LanInputShare channel proof v1\x00" + channel_binding + challenge


def verify_signature(certificate_der: bytes, challenge: bytes, signature: bytes) -> None:
    certificate = x509.load_der_x509_certificate(certificate_der)
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError("身份公钥类型无效")
    try:
        public_key.verify(signature, challenge, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("签名验证失败") from exc


def _atomic_write_json(path: Path, payload: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
