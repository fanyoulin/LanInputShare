from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton

from lan_input_share.transfer.status import TransferState
from lan_input_share.ui.transfer_popup import FileTransferPopup
from lan_input_share.ui.view_model import TransferView


def transfer(
    transfer_id: str = "tx-1",
    state: TransferState = TransferState.ACTIVE,
    *,
    percent: int = 50,
    error: str | None = None,
) -> TransferView:
    return TransferView(
        transfer_id=transfer_id,
        state=state,
        file_name="archive.zip",
        bytes_transferred=5 * 1024 * 1024,
        total_bytes=10 * 1024 * 1024,
        percent=percent,
        bytes_per_second=2 * 1024 * 1024,
        error=error,
        can_cancel=state is TransferState.ACTIVE,
    )


def test_popup_is_tool_window_and_cancel_targets_current_transfer(qtbot) -> None:  # type: ignore[no-untyped-def]
    cancelled: list[str] = []
    popup = FileTransferPopup(cancelled.append, terminal_close_ms=20, failure_close_ms=30)
    qtbot.addWidget(popup)

    popup.update_transfer(transfer())

    assert popup.windowFlags() & Qt.WindowType.Tool
    assert popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    button = popup.findChild(QPushButton, "cancelTransferButton")
    assert button is not None and button.isEnabled()
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert cancelled == ["tx-1"]


def test_popup_formats_progress_without_exposing_raw_error(qtbot) -> None:  # type: ignore[no-untyped-def]
    popup = FileTransferPopup(lambda _transfer_id: None)
    qtbot.addWidget(popup)
    value = transfer(
        state=TransferState.FAILED,
        error="目标位置不可用",
    )

    popup.update_transfer(value)

    labels = "\n".join(label.text() for label in popup.findChildren(QLabel))
    progress = popup.findChild(QProgressBar, "transferProgressBar")
    assert "archive.zip" in labels
    assert "5.0 MB / 10.0 MB" in labels
    assert "2.0 MB/s" in labels
    assert "目标位置不可用" in labels
    assert progress is not None and progress.value() == 50


@pytest.mark.parametrize(
    "state", [TransferState.COMPLETE, TransferState.CANCELLED]
)
def test_success_and_cancel_auto_close(qtbot, state: TransferState) -> None:  # type: ignore[no-untyped-def]
    popup = FileTransferPopup(
        lambda _transfer_id: None,
        terminal_close_ms=20,
        failure_close_ms=40,
    )
    qtbot.addWidget(popup)
    popup.update_transfer(transfer(state=state, percent=100))
    assert popup.isVisible()

    qtbot.waitUntil(lambda: not popup.isVisible(), timeout=200)


def test_failure_uses_longer_close_delay_and_next_transfer_resets_timer(qtbot) -> None:  # type: ignore[no-untyped-def]
    popup = FileTransferPopup(
        lambda _transfer_id: None,
        terminal_close_ms=10,
        failure_close_ms=80,
    )
    qtbot.addWidget(popup)
    popup.update_transfer(
        transfer(state=TransferState.FAILED, error="传输失败")
    )
    qtbot.wait(20)
    assert popup.isVisible()

    popup.update_transfer(transfer("tx-2", TransferState.ACTIVE, percent=1))
    qtbot.wait(90)

    assert popup.isVisible()
    button = popup.findChild(QPushButton, "cancelTransferButton")
    assert button is not None and button.isEnabled()


def test_regular_progress_update_does_not_activate_window(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    popup = FileTransferPopup(lambda _transfer_id: None)
    qtbot.addWidget(popup)
    activations: list[bool] = []
    monkeypatch.setattr(popup, "activateWindow", lambda: activations.append(True))

    popup.update_transfer(transfer(percent=1))
    popup.update_transfer(transfer(percent=2))

    assert activations == []
