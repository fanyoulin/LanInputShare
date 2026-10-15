from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from lan_input_share.elevated.models import ApprovedClient
from lan_input_share.elevated.pipe_transport import NamedPipeServer, pipe_name_for
from lan_input_share.elevated.server import ElevatedInputHelper
from lan_input_share.input.win32_injector import Win32InputInjector
from lan_input_share.windows.process_identity import (
    current_user_sid,
    inspect_process_identity,
)

_CLIENT_FIELDS = {"path", "sha256", "user_sid", "protocol_version"}
_INSTALL_FIELDS = _CLIENT_FIELDS | {"helper_sha256", "helper_protocol_version"}
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_SID_PATTERN = re.compile(r"^S-(?:\d+-)+\d+$", re.IGNORECASE)


def load_approved_client(path: Path) -> ApprovedClient:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"approved-client manifest 无法读取：{exc}") from exc
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(_CLIENT_FIELDS),
        frozenset(_INSTALL_FIELDS),
    }:
        raise ValueError("approved-client manifest 字段无效")
    mapping = cast(dict[str, object], value)
    client_path = mapping["path"]
    sha256 = mapping["sha256"]
    user_sid = mapping["user_sid"]
    protocol_version = mapping["protocol_version"]
    if not isinstance(client_path, str) or not client_path:
        raise ValueError("approved-client manifest path 无效")
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        raise ValueError("approved-client manifest sha256 无效")
    if not isinstance(user_sid, str) or _SID_PATTERN.fullmatch(user_sid) is None:
        raise ValueError("approved-client manifest user_sid 无效")
    if not isinstance(protocol_version, int) or isinstance(protocol_version, bool):
        raise ValueError("approved-client manifest protocol_version 无效")
    return ApprovedClient(Path(client_path), sha256, user_sid, protocol_version)


def helper_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanInputShareInputHelper")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return 0
    if args.manifest is None:
        parser.error("--manifest is required")
    approved = load_approved_client(args.manifest)
    identity = inspect_process_identity(os.getpid())
    sid = current_user_sid()
    if approved.user_sid.casefold() != sid.casefold():
        raise RuntimeError("批准的客户端用户与助手当前用户不一致")
    server = NamedPipeServer(pipe_name_for(sid, identity.session_id), sid)
    helper = ElevatedInputHelper(
        approved,
        Win32InputInjector(),
        server,
        helper_session_id=identity.session_id,
    )
    try:
        return helper.run()
    except KeyboardInterrupt:
        helper.stop()
        return 0


if __name__ == "__main__":
    raise SystemExit(helper_main())


__all__ = ["helper_main", "load_approved_client"]
