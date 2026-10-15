from __future__ import annotations

from pathlib import Path

import pytest

from lan_input_share.elevated.authorization import authorize_client
from lan_input_share.elevated.models import (
    HELPER_PROTOCOL_VERSION,
    ApprovedClient,
    AuthorizationReason,
    ClientIdentity,
)

APPROVED_HASH = "A" * 64


def approved() -> ApprovedClient:
    return ApprovedClient(
        path=Path(r"C:\Program Files\LanInputShare\LanInputShare.exe"),
        sha256=APPROVED_HASH,
        user_sid="S-1-5-21-1000",
        protocol_version=HELPER_PROTOCOL_VERSION,
    )


def actual(**changes: object) -> ClientIdentity:
    values: dict[str, object] = {
        "pid": 1234,
        "path": Path(r"C:\Program Files\LanInputShare\LanInputShare.exe"),
        "sha256": APPROVED_HASH,
        "user_sid": "S-1-5-21-1000",
        "session_id": 2,
    }
    values.update(changes)
    return ClientIdentity(**values)  # type: ignore[arg-type]


def test_authorize_client_accepts_exact_approved_identity() -> None:
    decision = authorize_client(approved(), actual(), helper_session_id=2)

    assert decision.allowed is True
    assert decision.reason is AuthorizationReason.ALLOWED


@pytest.mark.parametrize(
    ("changes", "session_id", "reason"),
    [
        ({"path": Path(r"C:\Temp\LanInputShare.exe")}, 2, AuthorizationReason.PATH_MISMATCH),
        ({"sha256": "B" * 64}, 2, AuthorizationReason.HASH_MISMATCH),
        ({"user_sid": "S-1-5-21-2000"}, 2, AuthorizationReason.SID_MISMATCH),
        ({"session_id": 3}, 2, AuthorizationReason.SESSION_MISMATCH),
    ],
)
def test_authorize_client_rejects_changed_identity(
    changes: dict[str, object], session_id: int, reason: AuthorizationReason
) -> None:
    decision = authorize_client(approved(), actual(**changes), helper_session_id=session_id)

    assert decision.allowed is False
    assert decision.reason is reason


def test_authorize_client_rejects_protocol_version_mismatch() -> None:
    value = approved()
    mismatched = ApprovedClient(
        value.path,
        value.sha256,
        value.user_sid,
        HELPER_PROTOCOL_VERSION + 1,
    )

    decision = authorize_client(mismatched, actual(), helper_session_id=2)

    assert decision.allowed is False
    assert decision.reason is AuthorizationReason.VERSION_MISMATCH


def test_authorize_client_compares_windows_path_case_insensitively() -> None:
    decision = authorize_client(
        approved(),
        actual(path=Path(r"c:\program files\laninputshare\LANINPUTSHARE.EXE")),
        helper_session_id=2,
    )

    assert decision.allowed is True
