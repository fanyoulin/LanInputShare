from __future__ import annotations

import random


class ExponentialBackoff:
    """Bounded exponential retry delay with proportional jitter."""

    def __init__(
        self,
        initial: float = 0.25,
        maximum: float = 5.0,
        multiplier: float = 2.0,
        jitter: float = 0.2,
        random_source: random.Random | None = None,
    ) -> None:
        if initial <= 0 or maximum <= 0 or initial > maximum:
            raise ValueError("退避时间必须为正数，且 initial 不能大于 maximum")
        if multiplier < 1:
            raise ValueError("multiplier 不能小于 1")
        if not 0 <= jitter <= 1:
            raise ValueError("jitter 必须在 0 到 1 之间")
        self._initial = initial
        self._maximum = maximum
        self._multiplier = multiplier
        self._jitter = jitter
        self._random = random_source or random.Random()
        self._base = initial

    def next_delay(self) -> float:
        spread = self._base * self._jitter
        delay = self._random.uniform(self._base - spread, self._base + spread)
        self._base = min(self._maximum, self._base * self._multiplier)
        return min(self._maximum, max(0.0, delay))

    def reset(self) -> None:
        self._base = self._initial


__all__ = ["ExponentialBackoff"]
