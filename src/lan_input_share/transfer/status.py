from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TransferState(StrEnum):
    ACTIVE = "active"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class FileTransferStatus:
    transfer_id: str
    relative_path: str
    bytes_transferred: int
    total_bytes: int
    bytes_per_second: float
    state: TransferState
    error: str | None = None


__all__ = ["FileTransferStatus", "TransferState"]
