from __future__ import annotations

import os

from lan_input_share.elevated.models import (
    HELPER_PROTOCOL_VERSION,
    ApprovedClient,
    AuthorizationDecision,
    AuthorizationReason,
    ClientIdentity,
)


def authorize_client(
    approved: ApprovedClient,
    actual: ClientIdentity,
    helper_session_id: int,
) -> AuthorizationDecision:
    if approved.protocol_version != HELPER_PROTOCOL_VERSION:
        return _reject(AuthorizationReason.VERSION_MISMATCH)
    if _canonical_path(approved.path) != _canonical_path(actual.path):
        return _reject(AuthorizationReason.PATH_MISMATCH)
    if approved.sha256 != actual.sha256:
        return _reject(AuthorizationReason.HASH_MISMATCH)
    if approved.user_sid != actual.user_sid:
        return _reject(AuthorizationReason.SID_MISMATCH)
    if actual.session_id != helper_session_id:
        return _reject(AuthorizationReason.SESSION_MISMATCH)
    return AuthorizationDecision(True, AuthorizationReason.ALLOWED)


def _canonical_path(path: os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _reject(reason: AuthorizationReason) -> AuthorizationDecision:
    return AuthorizationDecision(False, reason)


__all__ = ["authorize_client"]
