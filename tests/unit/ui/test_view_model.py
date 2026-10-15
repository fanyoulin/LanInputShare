from __future__ import annotations

import threading

import pytest

from lan_input_share.config.models import AppConfig
from lan_input_share.core.models import DeviceRole, ScreenPosition
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.runtime.service import OverwritePromptRequest
from lan_input_share.security.identity import PeerHello
from lan_input_share.security.pairing import PairingRequest
from lan_input_share.transfer.preflight import PasteDecision
from lan_input_share.transfer.status import FileTransferStatus, TransferState
from lan_input_share.ui.view_model import (
    AppViewModel,
    ConnectionStatusView,
    HelperStatusView,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.paused = 0
        self.returned = 0
        self.cancelled: list[str] = []
        self.configured: list[AppConfig] = []
        self.helper_actions: list[str] = []

    def pause(self) -> None:
        self.paused += 1

    def return_local(self) -> None:
        self.returned += 1

    def cancel_transfer(self, transfer_id: str) -> None:
        self.cancelled.append(transfer_id)

    def configure(self, config: AppConfig) -> None:
        self.configured.append(config)

    def enable_input_helper(self) -> None:
        self.helper_actions.append("enable")

    def repair_input_helper(self) -> None:
        self.helper_actions.append("repair")

    def remove_input_helper(self) -> None:
        self.helper_actions.append("remove")


def test_first_run_requires_name_role_and_adapter_before_start(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    _ = qtbot

    assert view_model.can_start is False
    assert view_model.device_name
    assert view_model.adapter_id == "auto"
    view_model.set_role(DeviceRole.CONTROLLER)
    assert view_model.can_start is True


def test_existing_config_preserves_saved_name_and_adapter() -> None:
    view_model = AppViewModel(FakeRuntime())
    view_model.load_config(
        AppConfig(
            version=2,
            device_name="Saved-PC",
            role=DeviceRole.CLIENT,
            adapter_id="Wi-Fi",
            bind_ip=None,
            port=24870,
        )
    )

    assert view_model.device_name == "Saved-PC"
    assert view_model.adapter_id == "Wi-Fi"


def test_fresh_config_defaults_to_windows_computer_name_and_auto_adapter(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("COMPUTERNAME", "OFFICE-PC")

    view_model = AppViewModel(FakeRuntime())

    assert view_model.device_name == "OFFICE-PC"
    assert view_model.adapter_id == "auto"


@pytest.mark.parametrize(
    ("state", "text", "actions"),
    [
        (HelperState.NOT_INSTALLED, "未启用", (True, False, False)),
        (HelperState.RUNNING, "可用", (False, False, True)),
        (HelperState.SECURE_DESKTOP, "安全桌面", (False, False, True)),
        (HelperState.UNAVAILABLE, "暂不可用", (False, True, True)),
        (HelperState.REPAIR_REQUIRED, "需要修复", (False, True, True)),
    ],
)
def test_helper_status_view_exposes_five_user_states(
    state: HelperState,
    text: str,
    actions: tuple[bool, bool, bool],
) -> None:
    view_model = AppViewModel(FakeRuntime())
    changes: list[HelperStatusView] = []
    view_model.helper_status_changed.connect(changes.append)

    view_model.update_helper_status(HelperStatus(state, "detail"))

    assert text in changes[-1].text
    assert (
        changes[-1].can_enable,
        changes[-1].can_repair,
        changes[-1].can_remove,
    ) == actions


def test_helper_actions_call_explicit_runtime_operations() -> None:
    runtime = FakeRuntime()
    view_model = AppViewModel(runtime)

    view_model.enable_input_helper()
    view_model.repair_input_helper()
    view_model.remove_input_helper()

    assert runtime.helper_actions == ["enable", "repair", "remove"]


def test_controller_applies_version_two_cross_layout_config() -> None:
    runtime = FakeRuntime()
    view_model = AppViewModel(runtime)
    view_model.set_device_name("Desk")
    view_model.set_role(DeviceRole.CONTROLLER)
    view_model.set_adapter("auto")
    view_model.set_local_position(ScreenPosition.CENTER)
    view_model.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)

    view_model.apply_settings()

    assert runtime.configured[0].version == 2
    assert runtime.configured[0].local_position is ScreenPosition.CENTER
    assert runtime.configured[0].peers[0].host == "192.168.110.157"


def test_transfer_status_is_formatted_for_existing_progress_bar(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    changes = []
    view_model.transfer_changed.connect(changes.append)
    _ = qtbot

    view_model.update_transfer_status(
        FileTransferStatus(
            "tx-1",
            "archive.zip",
            5 * 1024 * 1024,
            20 * 1024 * 1024,
            10 * 1024 * 1024,
            TransferState.ACTIVE,
        )
    )

    assert changes[-1].percent == 25
    assert changes[-1].file_name == "archive.zip"
    assert changes[-1].bytes_transferred == 5 * 1024 * 1024
    assert changes[-1].total_bytes == 20 * 1024 * 1024
    assert changes[-1].bytes_per_second == 10 * 1024 * 1024
    assert changes[-1].state is TransferState.ACTIVE


def test_transfer_failure_replaces_raw_path_with_safe_category(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    changes = []
    view_model.transfer_changed.connect(changes.append)

    view_model.update_transfer_status(
        FileTransferStatus(
            "tx-private",
            "private/report.zip",
            10,
            100,
            0,
            TransferState.FAILED,
            r"C:\Users\secret\Desktop\report.zip 目标目录不可写",
        )
    )

    assert changes[-1].error == "目标位置不可用"
    assert "secret" not in repr(changes[-1])


def test_disconnected_cross_layout_cannot_start() -> None:
    view_model = AppViewModel(FakeRuntime())
    view_model.set_device_name("Desk")
    view_model.set_role(DeviceRole.CONTROLLER)
    view_model.set_adapter("auto")
    view_model.set_local_position(ScreenPosition.LEFT)
    view_model.set_peer(ScreenPosition.RIGHT, "192.168.110.157", 24870)

    assert view_model.can_start is False
    with pytest.raises(ValueError, match="不连通"):
        view_model.apply_settings()


def test_status_shows_source_ip_target_ip_adapter_latency_and_tls() -> None:
    view_model = AppViewModel(FakeRuntime())
    status = ConnectionStatusView(
        source_ip="192.168.1.10",
        target_ip="192.168.1.20",
        adapter="Ethernet 2",
        latency_ms=3.5,
        tls="TLS 1.3 / trusted",
    )

    view_model.update_connection_status(status)

    text = view_model.connection_status_text
    assert "192.168.1.10" in text
    assert "192.168.1.20" in text
    assert "Ethernet 2" in text
    assert "3.5 ms" in text
    assert "TLS 1.3 / trusted" in text


def test_pairing_answer_completes_background_decision_future(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    request = PairingRequest(
        "request-1", PeerHello("controller", "Desk", b"certificate"), "ABCD"
    )
    with qtbot.waitSignal(view_model.pairing_requested):
        decision = view_model.decide_pairing(request)

    view_model.answer_pairing("request-1", True)

    assert decision.result(timeout=0) is True


def test_overwrite_answer_completes_background_future_once(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    request = OverwritePromptRequest(
        "transfer-1", 7, tuple(f"file-{index}.txt" for index in range(5))
    )
    requested = []
    view_model.overwrite_requested.connect(requested.append)

    decision = view_model.decide_overwrite(request)
    view_model.answer_overwrite("transfer-1", PasteDecision.SKIP)
    view_model.answer_overwrite("transfer-1", PasteDecision.OVERWRITE)

    assert len(requested) == 1
    assert requested[0].conflict_count == 7
    assert len(requested[0].conflict_preview) == 5
    assert decision.result(timeout=0) is PasteDecision.SKIP


def test_second_overwrite_request_is_cancelled_without_replacing_first(
    qtbot,
) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    changes = []
    view_model.overwrite_requested.connect(changes.append)

    first = view_model.decide_overwrite(
        OverwritePromptRequest("first", 1, ("a.txt",))
    )
    second = view_model.decide_overwrite(
        OverwritePromptRequest("second", 1, ("b.txt",))
    )

    assert not first.done()
    assert second.result(timeout=0) is PasteDecision.CANCEL
    assert [change.transfer_id for change in changes] == ["first"]


def test_shutdown_cancels_pending_overwrite_future(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    decision = view_model.decide_overwrite(
        OverwritePromptRequest("transfer-1", 1, ("a.txt",))
    )

    view_model.shutdown()

    assert decision.result(timeout=0) is PasteDecision.CANCEL


def test_overwrite_request_crosses_worker_thread_without_creating_widgets(
    qtbot,
) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    requested = []
    view_model.overwrite_requested.connect(requested.append)
    decisions = []

    def worker() -> None:
        decisions.append(
            view_model.decide_overwrite(
                OverwritePromptRequest("threaded", 1, ("file.txt",))
            )
        )

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1)
    qtbot.waitUntil(lambda: len(requested) == 1)

    assert requested[0].transfer_id == "threaded"
    view_model.answer_overwrite("threaded", PasteDecision.OVERWRITE)
    assert decisions[0].result(timeout=0) is PasteDecision.OVERWRITE


def test_runtime_cancelled_future_releases_slot_for_next_prompt(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    requested = []
    view_model.overwrite_requested.connect(requested.append)
    first = view_model.decide_overwrite(
        OverwritePromptRequest("first", 1, ("first.txt",))
    )

    first.cancel()
    second = view_model.decide_overwrite(
        OverwritePromptRequest("second", 1, ("second.txt",))
    )

    assert not second.done()
    assert [item.transfer_id for item in requested] == ["first", "second"]
