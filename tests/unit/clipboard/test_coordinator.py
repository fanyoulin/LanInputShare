from lan_input_share.clipboard.coordinator import ClipboardCoordinator
from lan_input_share.clipboard.models import (
    BroadcastClipboard,
    ClipboardItem,
    WriteLocalClipboard,
)


def item(
    origin: str,
    sequence: int,
    text: str,
    *,
    generation: str = "",
) -> ClipboardItem:
    return ClipboardItem.text(origin, sequence, text, generation=generation)


def test_self_generated_clipboard_update_is_not_rebroadcast() -> None:
    coordinator = ClipboardCoordinator("desk")
    remote = item("laptop", 1, "你好 🌍")

    assert coordinator.remote_received(remote)[0] == WriteLocalClipboard(remote)
    assert coordinator.local_changed(remote) == ()


def test_same_text_from_different_origin_does_not_form_three_peer_loop() -> None:
    desk = ClipboardCoordinator("desk")
    laptop = ClipboardCoordinator("laptop")
    copied = item("left", 7, "same text")

    desk_actions = desk.remote_received(copied)
    assert BroadcastClipboard(copied, exclude_origin_id="left") in desk_actions
    assert laptop.remote_received(copied)
    assert laptop.local_changed(copied) == ()
    assert desk.remote_received(copied) == ()


def test_out_of_order_sequence_is_ignored_per_origin() -> None:
    coordinator = ClipboardCoordinator("desk")

    assert coordinator.remote_received(item("laptop", 10, "new"))
    assert coordinator.remote_received(item("laptop", 9, "old")) == ()
    assert coordinator.remote_received(item("other", 1, "independent"))


def test_restarted_origin_accepts_low_sequence_from_new_generation() -> None:
    coordinator = ClipboardCoordinator("desk")

    assert coordinator.remote_received(
        item("laptop", 300, "before restart", generation="run-one")
    )
    restarted = item("laptop", 1, "after restart", generation="run-two")

    assert coordinator.remote_received(restarted)[0] == WriteLocalClipboard(restarted)


def test_rapid_updates_leave_latest_text_on_all_online_peers() -> None:
    coordinator = ClipboardCoordinator("desk")
    updates = [item("laptop", sequence, f"value-{sequence}") for sequence in range(1, 50)]

    actions = [coordinator.remote_received(update) for update in updates]

    writes = [
        action
        for group in actions
        for action in group
        if isinstance(action, WriteLocalClipboard)
    ]
    assert writes[-1].item.payload == "value-49"
    assert coordinator.latest_sequence("laptop") == 49
