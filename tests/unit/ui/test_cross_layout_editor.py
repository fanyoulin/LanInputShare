from PySide6.QtWidgets import QComboBox

from lan_input_share.core.models import ScreenPosition
from lan_input_share.ui.cross_layout_editor import CrossLayoutEditor


def test_editor_exposes_five_chinese_cross_slots(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)

    assert [
        editor.slot_button(position).text().splitlines()[0]
        for position in ScreenPosition
    ] == [
        "上",
        "下",
        "左",
        "中",
        "右",
    ]


def test_local_and_peer_positions_can_be_configured_before_connection(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)

    editor.set_local_position(ScreenPosition.CENTER)
    editor.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)

    assert editor.local_position is ScreenPosition.CENTER
    assert editor.peers[ScreenPosition.LEFT] == ("192.168.110.157", 24870)
    assert "本机" in editor.slot_button(ScreenPosition.CENTER).text()
    assert "192.168.110.157" in editor.slot_button(ScreenPosition.LEFT).text()


def test_peer_cannot_occupy_local_slot(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_local_position(ScreenPosition.CENTER)

    try:
        editor.set_peer(ScreenPosition.CENTER, "192.168.110.157", 24870)
    except ValueError as exc:
        assert "本机位置" in str(exc)
    else:
        raise AssertionError("peer should not replace local slot")


def test_existing_peer_can_move_without_remove_and_reenter(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)

    editor.slot_button(ScreenPosition.LEFT).click()
    position_combo = editor.findChild(QComboBox, "peerPositionCombo")
    assert position_combo is not None
    position_combo.setCurrentIndex(position_combo.findData(ScreenPosition.RIGHT))
    editor.save_peer_button.click()

    assert editor.peers == {
        ScreenPosition.RIGHT: ("192.168.110.157", 24870)
    }


def test_port_editor_is_wide_enough_for_five_digits_and_arrows(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)

    editor.port_spin.setValue(65535)

    assert editor.port_spin.minimumWidth() >= 110


def test_failed_peer_move_restores_original_peer_and_status(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)
    editor.set_slot_status(ScreenPosition.LEFT, "Client", "connected")
    editor.slot_button(ScreenPosition.LEFT).click()
    editor.host_edit.setText("not-an-ip")
    editor.position_combo.setCurrentIndex(
        editor.position_combo.findData(ScreenPosition.RIGHT)
    )

    editor.save_peer_button.click()

    assert editor.peers == {
        ScreenPosition.LEFT: ("192.168.110.157", 24870)
    }
    assert "Client · 已连接" in editor.slot_button(ScreenPosition.LEFT).text()


def test_connection_statuses_are_displayed_in_clear_chinese(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)

    editor.set_slot_status(ScreenPosition.LEFT, "", "authorization_required")
    assert "等待客户端确认" in editor.slot_button(ScreenPosition.LEFT).text()

    editor.set_slot_status(ScreenPosition.LEFT, "", "busy")
    assert "客户端使用中" in editor.slot_button(ScreenPosition.LEFT).text()


def test_connected_peer_slot_hides_position_label_to_fit_two_lines(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)

    editor.set_slot_status(ScreenPosition.LEFT, "Client", "connected")

    assert editor.slot_button(ScreenPosition.LEFT).text().splitlines() == [
        "192.168.110.157",
        "Client · 已连接",
    ]


def test_elevated_capability_uses_compact_slot_status(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = CrossLayoutEditor()
    qtbot.addWidget(editor)
    editor.set_peer(ScreenPosition.LEFT, "192.168.31.129", 24870)

    editor.set_slot_status(
        ScreenPosition.LEFT,
        "Client-157c",
        "增强控制可用：高权限输入助手可用",
    )

    assert editor.slot_button(ScreenPosition.LEFT).text().splitlines() == [
        "192.168.31.129",
        "Client-157c · 增强",
    ]
