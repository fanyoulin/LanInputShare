from __future__ import annotations

import hashlib
from pathlib import Path

from lan_input_share.windows import process_identity


class FakeApi:
    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def process_path(self, pid: int) -> Path:
        assert pid == 321
        return self.executable

    def process_user_sid(self, pid: int) -> str:
        assert pid == 321
        return "S-1-5-21-1000"

    def process_session_id(self, pid: int) -> int:
        assert pid == 321
        return 4

    def current_user_sid(self) -> str:
        return "S-1-5-21-1000"

    def input_desktop_name(self) -> str | None:
        return "Default"


def test_inspect_process_identity_reads_canonical_path_hash_sid_and_session(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "LanInputShare.exe"
    executable.write_bytes(b"trusted build")
    api = FakeApi(executable)

    identity = process_identity.inspect_process_identity(321, api=api)

    assert identity.pid == 321
    assert identity.path == executable.resolve()
    assert identity.sha256 == hashlib.sha256(b"trusted build").hexdigest().upper()
    assert identity.user_sid == "S-1-5-21-1000"
    assert identity.session_id == 4


def test_current_user_sid_uses_windows_identity_api(tmp_path: Path) -> None:
    assert process_identity.current_user_sid(api=FakeApi(tmp_path)) == "S-1-5-21-1000"


def test_default_input_desktop_name_returns_api_value(tmp_path: Path) -> None:
    assert process_identity.default_input_desktop_name(api=FakeApi(tmp_path)) == "Default"
