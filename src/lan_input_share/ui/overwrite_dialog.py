from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lan_input_share.transfer.preflight import PasteDecision
from lan_input_share.ui.view_model import OverwritePromptView


class OverwriteDialog(QDialog):
    answered = Signal(str, object)

    def __init__(
        self, view: OverwritePromptView, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._answered = False
        self.setWindowTitle("发现同名文件")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        summary = QLabel(f"目标位置已有 {view.conflict_count} 个同名或冲突项目。")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        preview_text = "\n".join(f"• {path}" for path in view.conflict_preview)
        preview = QLabel(preview_text)
        preview.setObjectName("conflictPreviewLabel")
        preview.setWordWrap(True)
        preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(preview)

        explanation = QLabel(
            "覆盖全部会替换冲突项目；跳过冲突只传输其余项目。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        buttons = QHBoxLayout()
        overwrite = QPushButton("覆盖全部")
        overwrite.setObjectName("overwriteAllButton")
        skip = QPushButton("跳过冲突")
        skip.setObjectName("skipConflictsButton")
        cancel = QPushButton("取消传输")
        cancel.setObjectName("cancelTransferButton")
        buttons.addWidget(overwrite)
        buttons.addWidget(skip)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

        overwrite.clicked.connect(
            lambda: self._answer(PasteDecision.OVERWRITE)
        )
        skip.clicked.connect(lambda: self._answer(PasteDecision.SKIP))
        cancel.clicked.connect(lambda: self._answer(PasteDecision.CANCEL))

    def reject(self) -> None:
        self._answer(PasteDecision.CANCEL)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._answered:
            self._answered = True
            self.answered.emit(
                self._view.transfer_id, PasteDecision.CANCEL
            )
        event.accept()

    def _answer(self, decision: PasteDecision) -> None:
        if self._answered:
            return
        self._answered = True
        self.answered.emit(self._view.transfer_id, decision)
        if decision is PasteDecision.CANCEL:
            super().reject()
        else:
            super().accept()


__all__ = ["OverwriteDialog"]
