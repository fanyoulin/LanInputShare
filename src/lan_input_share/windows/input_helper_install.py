from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import win32security  # type: ignore[import-untyped]

from lan_input_share.elevated.models import HELPER_PROTOCOL_VERSION, HelperState
from lan_input_share.windows.process_identity import current_user_sid

INPUT_HELPER_TASK_NAME = "LanInputShare Elevated Input Helper"
INPUT_HELPER_EXE_NAME = "LanInputShareInputHelper.exe"
INPUT_HELPER_MANIFEST_NAME = "approved-client.json"
_MANIFEST_FIELDS = {
    "path",
    "sha256",
    "user_sid",
    "protocol_version",
    "helper_sha256",
    "helper_protocol_version",
}
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True, slots=True)
class HelperInstallStatus:
    state: HelperState
    installed_path: Path | None
    detail: str


class InputHelperManager:
    def __init__(
        self,
        *,
        program_files: Path | None = None,
        user_sid: str | None = None,
        account_name: str | None = None,
        command_runner: Callable[..., object] = subprocess.run,
        protect_file: Callable[[Path, str], None] | None = None,
    ) -> None:
        root = program_files or Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        self.install_dir = root / "LanInputShare"
        self.installed_helper = self.install_dir / INPUT_HELPER_EXE_NAME
        self.manifest_path = self.install_dir / INPUT_HELPER_MANIFEST_NAME
        self._user_sid = user_sid or current_user_sid()
        self._account_name = account_name or _current_account_name()
        self._run = command_runner
        self._protect_file = protect_file or _protect_installed_file

    def status(self, main_exe: Path, helper_source: Path) -> HelperInstallStatus:
        if not self.installed_helper.exists() and not self.manifest_path.exists():
            return HelperInstallStatus(
                HelperState.NOT_INSTALLED,
                None,
                "高权限输入助手尚未安装",
            )
        if not self.installed_helper.is_file() or not self.manifest_path.is_file():
            return self._repair("助手安装文件不完整")
        try:
            manifest = self._read_manifest()
            expected_main = main_exe.resolve(strict=True)
            expected_helper = helper_source.resolve(strict=True)
            checks = (
                Path(str(manifest["path"])).resolve(strict=False) == expected_main,
                manifest["sha256"] == _sha256_file(expected_main),
                str(manifest["user_sid"]).casefold() == self._user_sid.casefold(),
                manifest["protocol_version"] == HELPER_PROTOCOL_VERSION,
                manifest["helper_sha256"] == _sha256_file(expected_helper),
                manifest["helper_protocol_version"] == HELPER_PROTOCOL_VERSION,
                manifest["helper_sha256"] == _sha256_file(self.installed_helper),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            return self._repair("助手安装清单无效")
        if not all(checks):
            return self._repair("主程序或助手版本已变化，需要修复")
        return HelperInstallStatus(
            HelperState.RUNNING,
            self.installed_helper,
            "高权限输入助手已安装",
        )

    def install(self, main_exe: Path, helper_source: Path) -> None:
        main_path = main_exe.resolve(strict=True)
        source_path = helper_source.resolve(strict=True)
        helper_hash = _sha256_file(source_path)
        if source_path.is_relative_to(self.install_dir.resolve(strict=False)):
            try:
                approved_hash = str(self._read_manifest()["helper_sha256"])
            except (OSError, ValueError, json.JSONDecodeError, KeyError):
                approved_hash = ""
            if helper_hash != approved_hash:
                raise ValueError("Program Files 内的助手来源不是已批准构建产物")

        self.install_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        staged_helper = self.install_dir / f".{INPUT_HELPER_EXE_NAME}.{token}.tmp"
        staged_manifest = self.install_dir / f".{INPUT_HELPER_MANIFEST_NAME}.{token}.tmp"
        backup_helper = self.install_dir / f".{INPUT_HELPER_EXE_NAME}.{token}.bak"
        backup_manifest = self.install_dir / f".{INPUT_HELPER_MANIFEST_NAME}.{token}.bak"
        manifest = {
            "path": str(main_path),
            "sha256": _sha256_file(main_path),
            "user_sid": self._user_sid,
            "protocol_version": HELPER_PROTOCOL_VERSION,
            "helper_sha256": helper_hash,
            "helper_protocol_version": HELPER_PROTOCOL_VERSION,
        }
        had_helper = self.installed_helper.exists()
        had_manifest = self.manifest_path.exists()
        try:
            shutil.copy2(source_path, staged_helper)
            staged_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._protect_file(staged_helper, self._user_sid)
            self._protect_file(staged_manifest, self._user_sid)
            if had_helper:
                os.replace(self.installed_helper, backup_helper)
            if had_manifest:
                os.replace(self.manifest_path, backup_manifest)
            os.replace(staged_helper, self.installed_helper)
            os.replace(staged_manifest, self.manifest_path)
            self._create_task()
        except BaseException:
            self._unlink_if_exists(self.installed_helper)
            self._unlink_if_exists(self.manifest_path)
            if backup_helper.exists():
                os.replace(backup_helper, self.installed_helper)
            if backup_manifest.exists():
                os.replace(backup_manifest, self.manifest_path)
            raise
        finally:
            for path in (staged_helper, staged_manifest, backup_helper, backup_manifest):
                self._unlink_if_exists(path)

    def start(self) -> None:
        self._run_command(
            ["schtasks.exe", "/Run", "/TN", INPUT_HELPER_TASK_NAME]
        )

    def remove(self) -> None:
        self._run_command(
            ["schtasks.exe", "/End", "/TN", INPUT_HELPER_TASK_NAME],
            check=False,
        )
        self._run_command(
            ["schtasks.exe", "/Delete", "/TN", INPUT_HELPER_TASK_NAME, "/F"],
            check=False,
        )
        self._unlink_if_exists(self.manifest_path)
        self._unlink_if_exists(self.installed_helper)

    def _create_task(self) -> None:
        action = subprocess.list2cmdline(
            [str(self.installed_helper), "--manifest", str(self.manifest_path)]
        )
        self._run_command(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                INPUT_HELPER_TASK_NAME,
                "/TR",
                action,
                "/SC",
                "ONLOGON",
                "/RU",
                self._account_name,
                "/RL",
                "HIGHEST",
                "/IT",
                "/F",
            ]
        )

    def _run_command(self, args: Sequence[str], *, check: bool = True) -> None:
        self._run(
            list(args),
            check=check,
            shell=False,
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )

    def _read_manifest(self) -> dict[str, object]:
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
            raise ValueError("助手安装清单字段无效")
        return {str(key): item for key, item in value.items()}

    def _repair(self, detail: str) -> HelperInstallStatus:
        return HelperInstallStatus(
            HelperState.REPAIR_REQUIRED,
            self.installed_helper if self.installed_helper.exists() else None,
            detail,
        )

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _current_account_name() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def _protect_installed_file(path: Path, user_sid: str) -> None:
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;{user_sid})"
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl,
        win32security.SDDL_REVISION_1,
    )
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )


__all__ = [
    "INPUT_HELPER_EXE_NAME",
    "INPUT_HELPER_MANIFEST_NAME",
    "INPUT_HELPER_TASK_NAME",
    "HelperInstallStatus",
    "InputHelperManager",
]
