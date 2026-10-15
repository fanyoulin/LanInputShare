from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lan_input_share.elevated.models import HELPER_PROTOCOL_VERSION, HelperState
from lan_input_share.windows.input_helper_install import InputHelperManager


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.task_xml = ""
        self.fail_create = False

    def __call__(self, args: list[str], **kwargs: Any) -> object:
        self.calls.append((list(args), dict(kwargs)))
        if self.fail_create and "/Create" in args:
            raise RuntimeError("task create failed")
        if "/XML" in args:
            self.task_xml = Path(args[args.index("/XML") + 1]).read_text(
                encoding="utf-16"
            )
        return object()


def manager(
    tmp_path: Path,
    runner: RecordingRunner,
    stopped_paths: list[Path] | None = None,
    stage_helper: Any = None,
) -> InputHelperManager:
    return InputHelperManager(
        program_files=tmp_path / "Program Files",
        user_sid="S-1-5-21-1001",
        account_name=r"DESKTOP\tester",
        command_runner=runner,
        protect_file=lambda _path, _sid: None,
        stop_processes=(stopped_paths.append if stopped_paths is not None else None),
        stage_helper=stage_helper,
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
    assert create_args[:4] == ["schtasks.exe", "/Create", "/TN", subject.task_name]
    assert "/XML" in create_args
    assert "<UserId>S-1-5-21-1001</UserId>" in runner.task_xml
    assert "<LogonType>InteractiveToken</LogonType>" in runner.task_xml
    assert "<RunLevel>HighestAvailable</RunLevel>" in runner.task_xml
    assert "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>" in runner.task_xml
    assert "<RestartOnFailure>" in runner.task_xml
    assert runner.task_xml.index("<Count>3</Count>") < runner.task_xml.index(
        "<Interval>PT1M</Interval>"
    )
    assert str(subject.installed_helper) in runner.task_xml
    assert str(subject.manifest_path) in runner.task_xml
    assert not list(subject.install_dir.glob(".input-helper-task.*.xml"))
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


def test_install_stops_orphaned_helper_before_replacing_files(tmp_path: Path) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main")
    _write(helper_source, b"helper")
    stopped_paths: list[Path] = []
    runner = RecordingRunner()
    subject = manager(tmp_path, runner, stopped_paths)

    subject.install(main_exe, helper_source)

    assert stopped_paths == [subject.installed_helper]
    assert runner.calls[0][0] == [
        "schtasks.exe",
        "/End",
        "/TN",
        subject.task_name,
    ]
    assert runner.calls[0][1]["check"] is False


def test_remove_stops_orphaned_helper_before_deleting_files(tmp_path: Path) -> None:
    runner = RecordingRunner()
    stopped_paths: list[Path] = []
    subject = manager(tmp_path, runner, stopped_paths)
    _write(subject.installed_helper, b"helper")
    _write(subject.manifest_path, b"{}")

    subject.remove()

    assert stopped_paths == [subject.installed_helper]
    assert [call[0] for call in runner.calls] == [
        ["schtasks.exe", "/End", "/TN", subject.task_name],
        ["schtasks.exe", "/Delete", "/TN", subject.task_name, "/F"],
    ]
    assert not subject.installed_helper.exists()
    assert not subject.manifest_path.exists()


def test_install_is_namespaced_per_windows_user(tmp_path: Path) -> None:
    subject = manager(tmp_path, RecordingRunner())

    assert subject.install_dir.name == "S-1-5-21-1001"
    assert "S-1-5-21-1001" in subject.task_name


def test_manifest_hash_is_computed_from_exclusive_staged_bytes(tmp_path: Path) -> None:
    main_exe = tmp_path / "release" / "LanInputShare.exe"
    helper_source = tmp_path / "release" / "LanInputShareInputHelper.exe"
    _write(main_exe, b"main")
    _write(helper_source, b"source-before-race")

    def stage(_source: Path, target: Path) -> str:
        target.write_bytes(b"bytes-actually-copied")
        return _sha256(target)

    subject = manager(tmp_path, RecordingRunner(), stage_helper=stage)
    subject.install(main_exe, helper_source)
    manifest = json.loads(subject.manifest_path.read_text(encoding="utf-8"))

    assert subject.installed_helper.read_bytes() == b"bytes-actually-copied"
    assert manifest["helper_sha256"] == _sha256(subject.installed_helper)


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
