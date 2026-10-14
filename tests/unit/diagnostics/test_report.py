from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lan_input_share.diagnostics.report import DiagnosticReportBuilder
from lan_input_share.network.adapters import AdapterInfo


def test_diagnostic_archive_contains_route_adapter_version_and_redacted_config(
    tmp_path: Path,
) -> None:
    log = tmp_path / "app.log"
    log.write_text("connected token=topsecret clipboard=private text\n", encoding="utf-8")
    builder = DiagnosticReportBuilder(
        version="0.1.0",
        config={
            "device_name": "Desk",
            "role": "controller",
            "adapter_id": "Ethernet",
            "private_key": "DO NOT INCLUDE",
        },
        adapters=(AdapterInfo("eth", "Ethernet", 7, "192.168.1.10", True, 10),),
        selected_route={"source": "192.168.1.10", "target": "192.168.1.20"},
        log_paths=(log,),
    )

    archive = builder.build(tmp_path / "diagnostics.zip")

    with zipfile.ZipFile(archive) as bundle:
        report = json.loads(bundle.read("report.json"))
        redacted_log = bundle.read("logs/app.log").decode("utf-8")
    assert report["version"] == "0.1.0"
    assert report["route"]["target"] == "192.168.1.20"
    assert report["adapters"][0]["classification"] == "physical"
    assert report["config"]["device_name"] == "Desk"
    assert "private_key" not in report["config"]
    assert "topsecret" not in redacted_log
    assert "private text" not in redacted_log


def test_diagnostic_archive_excludes_identity_key_clipboard_and_file_content(
    tmp_path: Path,
) -> None:
    identity_key = tmp_path / "identity-key.pem"
    copied_file = tmp_path / "copied.txt"
    identity_key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    copied_file.write_text("FILE BODY SECRET", encoding="utf-8")
    builder = DiagnosticReportBuilder(
        version="test",
        config={"clipboard_text": "CLIPBOARD SECRET", "password": "PASSWORD SECRET"},
        adapters=(),
        selected_route={},
        log_paths=(identity_key, copied_file),
    )

    archive = builder.build(tmp_path / "diagnostics.zip")

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        all_bytes = b"".join(bundle.read(name) for name in names)
    assert "logs/identity-key.pem" not in names
    assert "logs/copied.txt" not in names
    assert b"PRIVATE KEY MATERIAL" not in all_bytes
    assert b"CLIPBOARD SECRET" not in all_bytes
    assert b"FILE BODY SECRET" not in all_bytes


def test_diagnostics_include_slot_phase_but_truncate_identity_and_fingerprint(
    tmp_path: Path,
) -> None:
    builder = DiagnosticReportBuilder(
        version="test",
        config={"local_position": "center"},
        adapters=(),
        selected_route={"adapter": "Ethernet", "source": "192.168.110.156"},
        slot_states=(
            {
                "position": "left",
                "target_ip": "192.168.110.157",
                "status": "connected",
                "device_id": "12345678-1234-5678-1234-567812345678",
                "fingerprint": "full-secret-fingerprint",
            },
        ),
    )

    archive = builder.build(tmp_path / "diagnostics.zip")

    with zipfile.ZipFile(archive) as bundle:
        report = json.loads(bundle.read("report.json"))
    slot = report["slots"][0]
    assert report["config"]["local_position"] == "center"
    assert report["route"]["adapter"] == "Ethernet"
    assert slot["target_ip"] == "192.168.110.157"
    assert slot["status"] == "connected"
    assert slot["device_id"] == "12345678…"
    assert "fingerprint" not in slot
