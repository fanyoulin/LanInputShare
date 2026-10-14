from lan_input_share.input.motion import RemoteMotionAccumulator


def test_one_thousand_mouse_moves_preserve_total_relative_distance() -> None:
    accumulator = RemoteMotionAccumulator()

    for index in range(1_000):
        accumulator.add(1, -1, float(index))

    motion = accumulator.drain()
    assert motion is not None
    assert (motion.dx, motion.dy, motion.timestamp) == (1_000, -1_000, 999.0)
    assert accumulator.drain() is None


def test_reverse_motion_is_combined_in_arrival_order_without_stale_queueing() -> None:
    accumulator = RemoteMotionAccumulator()

    accumulator.add(100, 0, 1.0)
    accumulator.add(-140, 0, 2.0)

    motion = accumulator.drain()
    assert motion is not None
    assert (motion.dx, motion.dy, motion.timestamp) == (-40, 0, 2.0)
