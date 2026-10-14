from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelativeMotion:
    dx: int
    dy: int
    timestamp: float


class RemoteMotionAccumulator:
    def __init__(self) -> None:
        self._dx = 0
        self._dy = 0
        self._timestamp = 0.0
        self._lock = threading.Lock()

    def add(self, dx: int, dy: int, timestamp: float) -> None:
        with self._lock:
            self._dx += dx
            self._dy += dy
            self._timestamp = timestamp

    def drain(self) -> RelativeMotion | None:
        with self._lock:
            if self._dx == 0 and self._dy == 0:
                return None
            motion = RelativeMotion(self._dx, self._dy, self._timestamp)
            self._dx = 0
            self._dy = 0
            return motion

    def has_pending(self) -> bool:
        with self._lock:
            return self._dx != 0 or self._dy != 0

    def clear(self) -> None:
        with self._lock:
            self._dx = 0
            self._dy = 0


__all__ = ["RelativeMotion", "RemoteMotionAccumulator"]
