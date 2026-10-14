import pytest

import lan_input_share.protocol.messages as messages
from lan_input_share.protocol.codec import ProtocolError
from lan_input_share.protocol.messages import (
    PROTOCOL_VERSION,
    AuthChallengeMessage,
    AuthResponseMessage,
    ClipboardMessage,
    HelloMessage,
    MessageType,
    PairingDecisionMessage,
    ReleaseInputMessage,
    RoleConflictMessage,
    message_from_mapping,
)


def test_protocol_version_is_two_for_preflight_handshake() -> None:
    assert PROTOCOL_VERSION == 2


def test_file_paste_preflight_messages_round_trip() -> None:
    probe_type = getattr(messages.MessageType, "FILE_PASTE_PROBE", None)
    decision_type = getattr(messages.MessageType, "FILE_PASTE_DECISION", None)
    probe_class = getattr(messages, "FilePasteProbeMessage", None)
    decision_class = getattr(messages, "FilePasteDecisionMessage", None)

    assert probe_type is not None
    assert decision_type is not None
    assert probe_class is not None
    assert decision_class is not None

    probe = probe_class(
        "session-a",
        8,
        {
            "transfer_id": "transfer-a",
            "source_id": "source-a",
            "target_id": "target-a",
            "manifest": {"transfer_id": "transfer-a", "entries": []},
        },
    )
    decision = decision_class(
        "session-a",
        9,
        {
            "transfer_id": "transfer-a",
            "decision": "skip",
            "skipped_paths": ["folder/existing.zip"],
            "compatibility": False,
        },
    )

    assert message_from_mapping(probe.to_mapping()) == probe
    assert message_from_mapping(decision.to_mapping()) == decision


def test_protocol_declares_on_demand_file_clipboard_messages() -> None:
    assert {
        "file_offer",
        "file_request",
        "file_data",
        "file_abort",
        "file_paste_probe",
        "file_paste_decision",
    } <= {message_type.value for message_type in MessageType}


@pytest.mark.parametrize(
    "message",
    [
        HelloMessage("s", 1, {"device_id": "a", "role": "client"}),
        AuthChallengeMessage("s", 2, {"challenge": "YWJj"}),
        AuthResponseMessage("s", 3, {"certificate": "cert", "signature": "sig"}),
        PairingDecisionMessage("s", 4, {"approved": True}),
        ReleaseInputMessage("s", 5, {"reason": "disconnect"}),
        ClipboardMessage("s", 6, {"kind": "text", "digest": "abc"}),
        RoleConflictMessage("s", 7, {"controller_id": "main"}),
    ],
)
def test_message_mapping_round_trip_preserves_concrete_type(message):
    assert message_from_mapping(message.to_mapping()) == message


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "session_id": "",
            "sequence": 1,
            "payload": {},
        },
        {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "session_id": "s",
            "sequence": -1,
            "payload": {},
        },
        {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "session_id": "s",
            "sequence": 1,
            "payload": [],
        },
    ],
)
def test_message_mapping_rejects_schema_errors(mapping):
    with pytest.raises(ProtocolError, match="消息结构"):
        message_from_mapping(mapping)
