from __future__ import annotations

import json
import platform
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lan_input_share.config.models import AppConfig
from lan_input_share.network.adapters import AdapterInfo, classify_adapter

_SENSITIVE_KEYS = (
    "private_key",
    "password",
    "secret",
    "token",
    "clipboard",
    "file_content",
    "fingerprint",
)
_LOG_SECRET = re.compile(
    r"(?i)\b(token|password|secret|clipboard|private[_ -]?key)\s*[:=]\s*.*?"
    r"(?=\s+\w[\w -]*\s*[:=]|$)"
)


class DiagnosticReportBuilder:
    def __init__(
        self,
        *,
        version: str,
        config: AppConfig | Mapping[str, Any],
        adapters: Sequence[AdapterInfo],
        selected_route: Mapping[str, Any],
        slot_states: Sequence[Mapping[str, Any]] = (),
        log_paths: Sequence[Path] = (),
    ) -> None:
        self._version = version
        self._config = config.to_dict() if isinstance(config, AppConfig) else dict(config)
        self._adapters = tuple(adapters)
        self._route = dict(selected_route)
        self._slot_states = tuple(slot_states)
        self._log_paths = tuple(log_paths)

    def build(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "version": self._version,
            "os": platform.platform(),
            "config": _redact_mapping(self._config),
            "route": _redact_mapping(self._route),
            "slots": [_sanitize_slot(slot) for slot in self._slot_states],
            "adapters": [
                asdict(adapter) | {"classification": classify_adapter(adapter).value}
                for adapter in self._adapters
            ],
        }
        files: dict[str, bytes] = {
            "report.json": (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        }
        for path in self._log_paths:
            if path.suffix.casefold() != ".log" or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")[-500_000:]
            files[f"logs/{path.name}"] = _redact_log(text).encode("utf-8")
        _validate_no_secrets(files)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, files[name])
        return destination


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.casefold()
        if any(sensitive in lowered for sensitive in _SENSITIVE_KEYS):
            continue
        if isinstance(item, Mapping):
            result[key] = _redact_mapping(item)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, (list, tuple)):
            result[key] = [
                _redact_mapping(entry) if isinstance(entry, Mapping) else entry
                for entry in item
                if isinstance(entry, (Mapping, str, int, float, bool))
                or entry is None
            ]
    return result


def _sanitize_slot(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _redact_mapping(value)
    device_id = result.get("device_id")
    if isinstance(device_id, str) and len(device_id) > 8:
        result["device_id"] = f"{device_id[:8]}…"
    return result


def _redact_log(text: str) -> str:
    return _LOG_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


def _validate_no_secrets(files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        lowered = content.lower()
        if b"private key material" in lowered or b"clipboard secret" in lowered:
            raise ValueError(f"诊断文件仍包含敏感内容：{name}")


__all__ = ["DiagnosticReportBuilder"]
