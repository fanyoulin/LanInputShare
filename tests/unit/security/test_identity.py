import ssl

from lan_input_share.config.paths import AppPaths
from lan_input_share.security.identity import (
    DeviceIdentity,
    certificate_fingerprint,
    verify_signature,
)
from lan_input_share.security.tls import create_server_ssl_context


def _paths(root):
    return AppPaths(
        root=root,
        config_file=root / "config.json",
        log_dir=root / "logs",
        identity_dir=root / "identity",
        staging_dir=root / "staging",
    )


def test_identity_is_stable_across_reloads(tmp_path):
    paths = _paths(tmp_path)

    first = DeviceIdentity.load_or_create(paths, "desk-main")
    second = DeviceIdentity.load_or_create(paths, "renamed-later")

    assert second.device_id == first.device_id
    assert second.certificate_der == first.certificate_der
    verify_signature(first.certificate_der, b"challenge", first.sign(b"challenge"))
    verify_signature(first.certificate_der, b"challenge", second.sign(b"challenge"))
    assert certificate_fingerprint(first.certificate_der) == first.fingerprint


def test_server_context_requires_tls_1_3_and_loads_identity(tmp_path):
    identity = DeviceIdentity.load_or_create(_paths(tmp_path), "desk-main")

    context = create_server_ssl_context(identity)

    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_NONE
