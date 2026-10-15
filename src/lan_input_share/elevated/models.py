from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

HELPER_PROTOCOL_VERSION = 1


class HelperState(StrEnum):
    NOT_INSTALLED = "not_installed"
    RUNNING = "running"
    REPAIR_REQUIRED = "repair_required"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    SECURE_DESKTOP = "secure_desktop"


@dataclass(frozen=True, slots=True)
class HelperStatus:
    state: HelperState
    detail: str


@dataclass(frozen=True, slots=True)
class ApprovedClient:
    path: Path
    sha256: str
    user_sid: str
    protocol_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", self.sha256.upper())


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    pid: int
    path: Path
    sha256: str
    user_sid: str
    session_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", self.sha256.upper())


class AuthorizationReason(StrEnum):
    ALLOWED = "allowed"
    PATH_MISMATCH = "path_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    SID_MISMATCH = "sid_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    VERSION_MISMATCH = "version_mismatch"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: AuthorizationReason


__all__ = [
    "HELPER_PROTOCOL_VERSION",
    "ApprovedClient",
    "AuthorizationDecision",
    "AuthorizationReason",
    "ClientIdentity",
    "HelperState",
    "HelperStatus",
]
