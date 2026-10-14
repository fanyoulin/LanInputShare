from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from lan_input_share.transfer.preflight import PasteDecision
from lan_input_share.ui.overwrite_dialog import OverwriteDialog
from lan_input_share.ui.view_model import OverwritePromptView


def make_view(count: int = 3) -> OverwritePromptView:
    return OverwritePromptView(
        "transfer-1",
        count,
        tuple(f"folder/file-{index}.txt" for index in range(min(count, 5))),
    )


@pytest.mark.parametrize(
    ("object_name", "expected"),
    [
        ("overwriteAllButton", PasteDecision.OVERWRITE),
        ("skipConflictsButton", PasteDecision.SKIP),
        ("cancelTransferButton", PasteDecision.CANCEL),
    ],
)
def test_dialog_buttons_return_exact_decision(
    qtbot, object_name: str, expected: PasteDecision
) -> None:  # type: ignore[no-untyped-def]
    dialog = OverwriteDialog(make_view())
    qtbot.addWidget(dialog)
    answers = []
    dialog.answered.connect(lambda _transfer_id, decision: answers.append(decision))
    dialog.show()

    button = dialog.findChild(QPushButton, object_name)
    assert button is not None
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert answers == [expected]


def test_titlebar_x_means_cancel(qtbot) -> None:  # type: ignore[no-untyped-def]
    dialog = OverwriteDialog(make_view())
    qtbot.addWidget(dialog)
    answers = []
    dialog.answered.connect(lambda _transfer_id, decision: answers.append(decision))
    dialog.show()

    dialog.close()

    assert answers == [PasteDecision.CANCEL]


def test_large_conflict_count_renders_only_bounded_preview(qtbot) -> None:  # type: ignore[no-untyped-def]
    dialog = OverwriteDialog(make_view(10_000))
    qtbot.addWidget(dialog)

    labels = dialog.findChildren(QLabel)
    text = "\n".join(label.text() for label in labels)
    assert "10000" in text
    assert "file-4.txt" in text
    assert "file-5.txt" not in text
    assert len(labels) <= 3


def test_dialog_can_show_while_parent_main_window_is_hidden(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QWidget

    hidden_parent = QWidget()
    qtbot.addWidget(hidden_parent)
    dialog = OverwriteDialog(make_view(), hidden_parent)
    qtbot.addWidget(dialog)

    dialog.show()

    assert hidden_parent.isHidden()
    assert dialog.isVisible()
