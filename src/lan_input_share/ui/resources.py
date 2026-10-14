from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap


def _icon_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str) and bundle_root:
        return Path(bundle_root) / "assets" / "lan-input-share.ico"
    return Path(__file__).resolve().parents[3] / "assets" / "lan-input-share.ico"


@lru_cache(maxsize=1)
def application_icon() -> QIcon:
    path = _icon_path()
    if path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return _fallback_icon()


def _fallback_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0B1F3A"))
    painter.drawRoundedRect(QRectF(1, 1, 30, 30), 7, 7)
    painter.setBrush(QColor("white"))
    painter.drawRoundedRect(QRectF(6, 7, 20, 14), 2, 2)
    painter.drawRect(QRectF(14, 21, 4, 4))
    painter.setBrush(QColor("#35D6E6"))
    painter.drawEllipse(QRectF(22, 5, 5, 5))
    painter.drawEllipse(QRectF(24, 13, 5, 5))
    painter.drawEllipse(QRectF(21, 21, 5, 5))
    painter.end()
    return QIcon(pixmap)


__all__ = ["application_icon"]
