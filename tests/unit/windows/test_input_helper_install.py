from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lan_input_share.elevated.models import HELPER_PROTOCOL_VERSION, HelperState
from lan_input_share.windows.input_helper_install import (
    INPUT_HELPER_TASK_NAME,
    InputHelperManager,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail_create = False

    def __call__(self, args: list[str], **kwargs: Any) -> object:
        self.calls.append((list(args), dict(kwargs)))
        if self.fail_create and "/Create" in args:
            raise RuntimeError("task create failed")
        return object()


def manager(tmp_path: Path, runner: RecordingRunner) -> InputHelperManager:
    return InputHelperManager(
        program_files=tmp_path / "Program Files",
        user_sid="S-1-5-21-1001",
        account_name=r"DESKTOP\tester",
        command_runner=runner,
        protect_file=lambda _path, _sid: None,
    )


def test_install_writes_atomic_artifacts_and_highest_interactive_task(
    tmp_path: Path,
) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main-v1")
    _write(helper_source, b"helper-v1")
    runner = RecordingRunner()
    subject = manager(tmp_path, runner)

    subject.install(main_exe, helper_source)

    assert subject.installed_helper.read_bytes() == b"helper-v1"
    manifest = json.loads(subject.manifest_path.read_text(encoding="utf-8"))
    assert manifest["path"] == str(main_exe.resolve())
    assert manifest["sha256"] == _sha256(main_exe)
    assert manifest["user_sid"] == "S-1-5-21-1001"
    assert manifest["protocol_version"] == HELPER_PROTOCOL_VERSION
    assert manifest["helper_sha256"] == _sha256(helper_source)
    assert manifest["helper_protocol_version"] == HELPER_PROTOCOL_VERSION
    create_args, options = runner.calls[-1]
    assert create_args[:4] == ["schtasks.exe", "/Create", "/TN", INPUT_HELPER_TASK_NAME]
    assert create_args[create_args.index("/RU") + 1] == r"DESKTOP\tester"
    assert "/IT" in create_args
    assert create_args[create_args.index("/RL") + 1] == "HIGHEST"
    assert options["shell"] is False


def test_valid_install_status_is_running_and_read_only(tmp_path: Path) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main")
    _write(helper_source, b"helper")
    runner = RecordingRunner()
    subject = manager(tmp_path, runner)
    subject.install(main_exe, helper_source)
    runner.calls.clear()

    status = subject.status(main_exe, helper_source)

    assert status.state is HelperState.RUNNING
    assert status.installed_path == subject.installed_helper
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", r"C:\Moved\LanInputShare.exe"),
        ("sha256", "0" * 64),
        ("helper_protocol_version", HELPER_PROTOCOL_VERSION + 1),
    ],
)
def test_changed_identity_or_version_requires_repair(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main")
    _write(helper_source, b"helper")
    runner = RecordingRunner()
    subject = manager(tmp_path, runner)
    subject.install(main_exe, helper_source)
    manifest = json.loads(subject.manifest_path.read_text(encoding="utf-8"))
    manifest[field] = replacement
    subject.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner.calls.clear()

    status = subject.status(main_exe, helper_source)

    assert status.state is HelperState.REPAIR_REQUIRED
    assert runner.calls == []


def test_failed_task_creation_rolls_back_previous_files(tmp_path: Path) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main-v1")
    _write(helper_source, b"helper-v1")
    runner = RecordingRunner()
    subject = manager(tmp_path, runner)
    subject.install(main_exe, helper_source)
    old_helper = subject.installed_helper.read_bytes()
    old_manifest = subject.manifest_path.read_bytes()
    _write(main_exe, b"main-v2")
    _write(helper_source, b"helper-v2")
    runner.fail_create = True

    with pytest.raises(RuntimeError, match="task create"):
        subject.install(main_exe, helper_source)

    assert subject.installed_helper.read_bytes() == old_helper
    assert subject.manifest_path.read_bytes() == old_manifest


def test_tampered_source_inside_install_directory_is_rejected(tmp_path: Path) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main")
    _write(helper_source, b"helper")
    subject = manager(tmp_path, RecordingRunner())
    subject.install(main_exe, helper_source)
    subject.installed_helper.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="批准"):
        subject.install(main_exe, subject.installed_helper)


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest().upper()
