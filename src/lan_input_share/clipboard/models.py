from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class ClipboardKind(StrEnum):
    TEXT = "text"
    FILES = "files"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class ClipboardImage:
    format: str
    data: bytes

    def __post_init__(self) -> None:
        if self.format not in {"dib", "dibv5"}:
            raise ValueError("不支持的 Windows 图片剪贴板格式")
        if not self.data:
            raise ValueError("图片剪贴板不能为空")


@dataclass(frozen=True, slots=True)
class ClipboardItem:
    origin_id: str
    sequence: int
    digest: str
    kind: ClipboardKind
    payload: str | tuple[str, ...] | ClipboardImage
    generation: str = ""

    def __post_init__(self) -> None:
        if not self.origin_id:
            raise ValueError("剪贴板来源不能为空")
        if self.sequence < 0:
            raise ValueError("剪贴板序号不能为负数")
        if len(self.digest) != 64:
            raise ValueError("剪贴板摘要必须是 SHA-256")

    @classmethod
    def text(
        cls,
        origin_id: str,
        sequence: int,
        text: str,
        *,
        generation: str = "",
    ) -> ClipboardItem:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(origin_id, sequence, digest, ClipboardKind.TEXT, text, generation)

    @classmethod
    def image(
        cls,
        origin_id: str,
        sequence: int,
        image: ClipboardImage,
        *,
        generation: str = "",
    ) -> ClipboardItem:
        digest = hashlib.sha256(
            image.format.encode("ascii") + b"\x00" + image.data
        ).hexdigest()
        return cls(origin_id, sequence, digest, ClipboardKind.IMAGE, image, generation)

    @property
    def marker(self) -> str:
        if self.generation:
            return f"{self.origin_id}:{self.generation}:{self.sequence}:{self.digest}"
        return f"{self.origin_id}:{self.sequence}:{self.digest}"


@dataclass(frozen=True, slots=True)
class BroadcastClipboard:
    item: ClipboardItem
    exclude_origin_id: str | None = None


@dataclass(frozen=True, slots=True)
class WriteLocalClipboard:
    item: ClipboardItem


type ClipboardAction = BroadcastClipboard | WriteLocalClipboard


__all__ = [
    "BroadcastClipboard",
    "ClipboardAction",
    "ClipboardImage",
    "ClipboardItem",
    "ClipboardKind",
    "WriteLocalClipboard",
]
