from __future__ import annotations

from lan_input_share.clipboard.models import (
    BroadcastClipboard,
    ClipboardAction,
    ClipboardItem,
    WriteLocalClipboard,
)


class ClipboardCoordinator:
    def __init__(self, local_device_id: str, *, forward_remote: bool = True) -> None:
        if not local_device_id:
            raise ValueError("本机设备 ID 不能为空")
        self._local_device_id = local_device_id
        self._forward_remote = forward_remote
        self._latest_by_origin: dict[tuple[str, str], int] = {}
        self._seen: set[tuple[str, str, int, str]] = set()
        self._pending_local_markers: set[tuple[str, str, int, str]] = set()

    def local_changed(self, item: ClipboardItem) -> tuple[ClipboardAction, ...]:
        key = _key(item)
        if key in self._pending_local_markers:
            self._pending_local_markers.remove(key)
            self._record(item)
            return ()
        if not self._accept(item):
            return ()
        return (BroadcastClipboard(item),)

    def remote_received(self, item: ClipboardItem) -> tuple[ClipboardAction, ...]:
        if not self._accept(item):
            return ()
        self._pending_local_markers.add(_key(item))
        actions: list[ClipboardAction] = [WriteLocalClipboard(item)]
        if self._forward_remote:
            actions.append(BroadcastClipboard(item, exclude_origin_id=item.origin_id))
        return tuple(actions)

    def latest_sequence(self, origin_id: str, generation: str = "") -> int | None:
        return self._latest_by_origin.get((origin_id, generation))

    def _accept(self, item: ClipboardItem) -> bool:
        key = _key(item)
        if key in self._seen:
            return False
        origin = (item.origin_id, item.generation)
        latest = self._latest_by_origin.get(origin, -1)
        if item.sequence <= latest:
            return False
        self._record(item)
        return True

    def _record(self, item: ClipboardItem) -> None:
        self._seen.add(_key(item))
        origin = (item.origin_id, item.generation)
        self._latest_by_origin[origin] = max(
            item.sequence, self._latest_by_origin.get(origin, -1)
        )


def _key(item: ClipboardItem) -> tuple[str, str, int, str]:
    return item.origin_id, item.generation, item.sequence, item.digest


__all__ = ["ClipboardCoordinator"]
