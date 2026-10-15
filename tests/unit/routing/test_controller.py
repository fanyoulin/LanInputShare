import pytest

from lan_input_share.core.models import (
    DeviceInfo,
    DeviceRole,
    ScreenInfo,
    ScreenPosition,
)
from lan_input_share.input.events import KeyEvent, MouseMoveEvent, Point
from lan_input_share.routing.controller import (
    ConfineLocalCursor,
    DeliverLocal,
    ReleaseTarget,
    RestoreLocalCursor,
    RoutingController,
    SendRemote,
    SwitchTarget,
)
from lan_input_share.routing.layout import CrossLayout, LinearLayout


def device(device_id: str, role: DeviceRole, width: int, height: int) -> DeviceInfo:
    return DeviceInfo(device_id, device_id, role, ScreenInfo(width, height))


def controller() -> RoutingController:
    devices = {
        "left": device("left", DeviceRole.CLIENT, 1280, 720),
        "desk": device("desk", DeviceRole.CONTROLLER, 1920, 1080),
        "right": device("right", DeviceRole.CLIENT, 2560, 1440),
    }
    return RoutingController("desk", devices, LinearLayout(("left", "desk", "right")))


def test_three_devices_route_left_to_middle_to_right_and_back() -> None:
    routing = controller()

    actions = routing.handle_input(MouseMoveEvent(Point(1919, 540)))
    assert actions[0] == SwitchTarget("desk", "right")
    assert ConfineLocalCursor() in actions
    assert actions[-1] == SendRemote("right", MouseMoveEvent(Point(0, 720)))

    routing.handle_input(MouseMoveEvent(Point(100, 720)))
    actions = routing.handle_input(MouseMoveEvent(Point(-1, 720)))
    assert SwitchTarget("right", "desk") in actions
    assert RestoreLocalCursor(Point(1919, 540)) in actions


def test_edge_hysteresis_prevents_immediate_bounce() -> None:
    routing = controller()
    routing.handle_input(MouseMoveEvent(Point(1919, 100)))

    actions = routing.handle_input(MouseMoveEvent(Point(0, 133)))

    assert not any(isinstance(action, SwitchTarget) for action in actions)
    assert actions == (SendRemote("right", MouseMoveEvent(Point(0, 133))),)


@pytest.mark.parametrize(
    ("edge", "expected"),
    [
        (Point(0, 540), Point(1279, 360)),
        (Point(1919, 540), Point(0, 720)),
    ],
)
def test_remote_entry_preserves_the_shared_edge_percentage(
    edge: Point, expected: Point
) -> None:
    routing = controller()

    actions = routing.handle_input(edge_event := MouseMoveEvent(edge))

    assert edge_event.position == edge
    assert isinstance(actions[-1], SendRemote)
    assert actions[-1].event == MouseMoveEvent(expected)


def test_reversing_after_entry_returns_at_the_same_percentage_position() -> None:
    routing = controller()

    entry = routing.handle_input(MouseMoveEvent(Point(0, 540)))
    assert entry[-1] == SendRemote("left", MouseMoveEvent(Point(1279, 360)))

    returned = routing.handle_input(MouseMoveEvent(Point(1280, 360)))

    assert SwitchTarget("left", "desk") in returned
    assert RestoreLocalCursor(Point(0, 540)) in returned


def test_entire_edge_crosses_by_percentage_to_a_shorter_screen() -> None:
    routing = controller()

    actions = routing.handle_input(MouseMoveEvent(Point(0, 900)))

    assert actions[-1] == SendRemote("left", MouseMoveEvent(Point(1279, 600)))
    assert routing.active_device_id == "left"


def test_tiny_remote_screen_does_not_skip_on_perpendicular_motion() -> None:
    devices = {
        "desk": device("desk", DeviceRole.CONTROLLER, 1920, 1080),
        "tiny": device("tiny", DeviceRole.CLIENT, 1, 1),
        "right": device("right", DeviceRole.CLIENT, 1920, 1080),
    }
    routing = RoutingController(
        "desk", devices, LinearLayout(("desk", "tiny", "right"))
    )

    entry = routing.handle_input(MouseMoveEvent(Point(1919, 0)))
    motion = routing.handle_input(MouseMoveEvent(Point(0, 1)))

    assert entry[-1] == SendRemote("tiny", MouseMoveEvent(Point(0, 0)))
    assert motion == (SendRemote("tiny", MouseMoveEvent(Point(0, 0))),)
    assert routing.active_device_id == "tiny"


def test_remote_pointer_is_clamped_at_an_edge_without_a_neighbor() -> None:
    routing = controller()
    routing.handle_input(MouseMoveEvent(Point(1919, 540)))

    actions = routing.handle_input(MouseMoveEvent(Point(4000, 720)))
    reversed_actions = routing.handle_input(MouseMoveEvent(Point(2558, 720)))

    assert actions == (SendRemote("right", MouseMoveEvent(Point(2559, 720))),)
    assert reversed_actions == (
        SendRemote("right", MouseMoveEvent(Point(2558, 720))),
    )


def test_disconnect_of_active_client_releases_inputs_and_returns_local() -> None:
    routing = controller()
    routing.activate("right")
    routing.handle_input(KeyEvent(30, 65, True))

    actions = routing.handle_disconnect("right")

    assert actions[0] == ReleaseTarget("right", (KeyEvent(30, 65, False),))
    assert SwitchTarget("right", "desk") in actions
    assert actions[-1] == RestoreLocalCursor()
    assert routing.active_device_id == "desk"


def test_route_to_top_client_and_return_to_controller() -> None:
    devices = {
        "desk": device("desk", DeviceRole.CONTROLLER, 1920, 1080),
        "top": device("top", DeviceRole.CLIENT, 2560, 1440),
    }
    routing = RoutingController(
        "desk",
        devices,
        CrossLayout(
            {ScreenPosition.CENTER: "desk", ScreenPosition.TOP: "top"}
        ),
    )

    actions = routing.handle_input(MouseMoveEvent(Point(960, 0)))
    assert actions[0] == SwitchTarget("desk", "top")
    assert actions[-1] == SendRemote("top", MouseMoveEvent(Point(1280, 1439)))

    routing.handle_input(MouseMoveEvent(Point(1280, 1300)))
    actions = routing.handle_input(MouseMoveEvent(Point(1280, 1440)))
    assert SwitchTarget("top", "desk") in actions
    assert RestoreLocalCursor(Point(960, 0)) in actions


@pytest.mark.parametrize(
    ("controller_position", "peer_position", "edge"),
    [
        (ScreenPosition.CENTER, ScreenPosition.TOP, Point(960, 0)),
        (ScreenPosition.CENTER, ScreenPosition.BOTTOM, Point(960, 1079)),
        (ScreenPosition.CENTER, ScreenPosition.LEFT, Point(0, 540)),
        (ScreenPosition.CENTER, ScreenPosition.RIGHT, Point(1919, 540)),
        (ScreenPosition.TOP, ScreenPosition.CENTER, Point(960, 1079)),
        (ScreenPosition.BOTTOM, ScreenPosition.CENTER, Point(960, 0)),
        (ScreenPosition.LEFT, ScreenPosition.CENTER, Point(1919, 540)),
        (ScreenPosition.RIGHT, ScreenPosition.CENTER, Point(0, 540)),
    ],
)
def test_controller_can_occupy_any_cross_position(
    controller_position: ScreenPosition,
    peer_position: ScreenPosition,
    edge: Point,
) -> None:
    devices = {
        "desk": device("desk", DeviceRole.CONTROLLER, 1920, 1080),
        "peer": device("peer", DeviceRole.CLIENT, 1920, 1080),
    }
    routing = RoutingController(
        "desk",
        devices,
        CrossLayout({controller_position: "desk", peer_position: "peer"}),
    )

    actions = routing.handle_input(MouseMoveEvent(edge))

    assert actions[0] == SwitchTarget("desk", "peer")


def test_edge_without_connected_neighbor_stays_local() -> None:
    devices = {"desk": device("desk", DeviceRole.CONTROLLER, 1920, 1080)}
    routing = RoutingController(
        "desk", devices, CrossLayout({ScreenPosition.CENTER: "desk"})
    )

    actions = routing.handle_input(MouseMoveEvent(Point(1919, 540)))

    assert actions == (DeliverLocal(MouseMoveEvent(Point(1919, 540))),)
