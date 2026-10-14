from __future__ import annotations

import random

from lan_input_share.network.backoff import ExponentialBackoff


def test_reconnect_backoff_is_bounded_and_jittered() -> None:
    backoff = ExponentialBackoff(
        initial=0.5,
        maximum=4.0,
        multiplier=2.0,
        jitter=0.25,
        random_source=random.Random(7),
    )

    delays = [backoff.next_delay() for _ in range(8)]

    assert all(0.0 <= delay <= 4.0 for delay in delays)
    assert delays[0] != 0.5
    assert delays[-1] != delays[-2]
    backoff.reset()
    assert 0.375 <= backoff.next_delay() <= 0.625
