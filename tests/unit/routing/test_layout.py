import pytest

from lan_input_share.core.models import ScreenInfo, ScreenPosition
from lan_input_share.routing.layout import (
    CrossLayout,
    Direction,
    LinearLayout,
    map_edge_position,
    map_horizontal_edge_position,
    map_vertical_edge_position,
)


def test_three_devices_route_left_to_middle_to_right_and_back() -> None:
    layout = LinearLayout(("left", "middle", "right"))

    assert layout.left_of("middle") == "left"
    assert layout.right_of("middle") == "right"
    assert layout.right_of("left") == "middle"
    assert layout.left_of("right") == "middle"


def test_edge_without_neighbor_does_not_switch() -> None:
    layout = LinearLayout(("left", "middle", "right"))

    assert layout.left_of("left") is None
    assert layout.right_of("right") is None


def test_vertical_position_maps_proportionally_between_resolutions() -> None:
    assert map_edge_position(ScreenInfo(1920, 1080), ScreenInfo(1366, 768), 0) == 0
    assert map_edge_position(ScreenInfo(1920, 1080), ScreenInfo(1366, 768), 1079) == 767
    assert map_edge_position(ScreenInfo(1920, 1080), ScreenInfo(1366, 768), 540) == 384


def test_duplicate_or_fourth_device_is_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        LinearLayout(("desk", "desk"))
    with pytest.raises(ValueError, match="1 到 3"):
        LinearLayout(("one", "two", "three", "four"))


def test_cross_layout_center_is_adjacent_to_all_outer_slots() -> None:
    layout = CrossLayout(
        {
            ScreenPosition.TOP: "top",
            ScreenPosition.LEFT: "left",
            ScreenPosition.CENTER: "desk",
            ScreenPosition.RIGHT: "right",
            ScreenPosition.BOTTOM: "bottom",
        }
    )

    assert layout.neighbor("desk", Direction.UP) == "top"
    assert layout.neighbor("desk", Direction.LEFT) == "left"
    assert layout.neighbor("desk", Direction.RIGHT) == "right"
    assert layout.neighbor("desk", Direction.DOWN) == "bottom"
    assert layout.neighbor("top", Direction.DOWN) == "desk"
    assert layout.neighbor("left", Direction.RIGHT) == "desk"
    assert layout.neighbor("right", Direction.LEFT) == "desk"
    assert layout.neighbor("bottom", Direction.UP) == "desk"


def test_cross_layout_outer_slots_are_not_adjacent_to_each_other() -> None:
    layout = CrossLayout(
        {
            ScreenPosition.TOP: "top",
            ScreenPosition.CENTER: "desk",
            ScreenPosition.RIGHT: "right",
        }
    )

    assert layout.neighbor("top", Direction.RIGHT) is None
    assert layout.neighbor("right", Direction.UP) is None


def test_cross_layout_rejects_disconnected_or_duplicate_devices() -> None:
    disconnected = CrossLayout(
        {ScreenPosition.LEFT: "left", ScreenPosition.RIGHT: "right"}
    )
    with pytest.raises(ValueError, match="不连通"):
        disconnected.validate_connected()

    with pytest.raises(ValueError, match="重复"):
        CrossLayout(
            {ScreenPosition.CENTER: "desk", ScreenPosition.RIGHT: "desk"}
        )


def test_horizontal_and_vertical_edge_coordinates_map_proportionally() -> None:
    source = ScreenInfo(1920, 1080)
    target = ScreenInfo(2560, 1440)

    assert map_horizontal_edge_position(source, target, 540) == 720
    assert map_vertical_edge_position(source, target, 960) == 1280


def test_edge_mapping_clamps_and_handles_single_pixel_screens() -> None:
    source = ScreenInfo(1, 1)
    target = ScreenInfo(5, 7)

    assert map_horizontal_edge_position(source, target, -100) == 0
    assert map_horizontal_edge_position(source, target, 100) == 0
    assert map_vertical_edge_position(source, target, -100) == 0
    assert map_vertical_edge_position(source, target, 100) == 0
