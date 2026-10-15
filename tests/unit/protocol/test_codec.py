import json
import struct

import pytest

from lan_input_share.protocol.codec import FrameDecoder, ProtocolError, encode_frame
from lan_input_share.protocol.messages import (
    PROTOCOL_VERSION,
    HeartbeatMessage,
    InputMessage,
)


def _heartbeat(sequence: int = 1) -> HeartbeatMessage:
    return HeartbeatMessage(
        session_id="session-a", sequence=sequence, payload={"sent_at_ns": 123}
    )


def test_decoder_accepts_one_byte_fragments():
    frame = encode_frame(_heartbeat())
    decoder = FrameDecoder()
    decoded = []

    for byte in frame:
        decoded.extend(decoder.feed(bytes([byte])))

    assert decoded == [_heartbeat()]


def test_decoder_returns_two_concatenated_frames():
    decoder = FrameDecoder()
    data = encode_frame(_heartbeat(1)) + encode_frame(_heartbeat(2))

    assert decoder.feed(data) == [_heartbeat(1), _heartbeat(2)]


def test_decoder_rejects_payload_larger_than_one_megabyte():
    decoder = FrameDecoder(max_payload=1_048_576)

    with pytest.raises(ProtocolError, match="超过限制"):
        decoder.feed(struct.pack(">I", 1_048_577))


def test_decoder_rejects_unknown_protocol_version():
    body = json.dumps(
        {
            "version": 99,
            "type": "heartbeat",
            "session_id": "s",
            "sequence": 1,
            "payload": {"sent_at_ns": 1},
        }
    ).encode()

    with pytest.raises(ProtocolError, match="协议版本"):
        FrameDecoder().feed(struct.pack(">I", len(body)) + body)


def test_decoder_rejects_previous_version_two_frame():
    body = json.dumps(
        {
            "version": 2,
            "type": "heartbeat",
            "session_id": "s",
            "sequence": 1,
            "payload": {"sent_at_ns": 1},
        }
    ).encode()

    with pytest.raises(ProtocolError, match="协议版本"):
        FrameDecoder().feed(struct.pack(">I", len(body)) + body)


def test_decoder_is_poisoned_after_malformed_json():
    decoder = FrameDecoder()
    malformed = b"{not-json"

    with pytest.raises(ProtocolError, match="JSON"):
        decoder.feed(struct.pack(">I", len(malformed)) + malformed)
    with pytest.raises(ProtocolError, match="不可继续使用"):
        decoder.feed(encode_frame(_heartbeat()))


def test_decoder_rejects_zero_length_and_unknown_type():
    with pytest.raises(ProtocolError, match="长度不能为零"):
        FrameDecoder().feed(struct.pack(">I", 0))

    body = json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": "not-real",
            "session_id": "s",
            "sequence": 1,
            "payload": {},
        }
    ).encode()
    with pytest.raises(ProtocolError, match="消息类型"):
        FrameDecoder().feed(struct.pack(">I", len(body)) + body)


def test_input_message_preserves_sequence_and_key_state():
    message = InputMessage(
        session_id="session-a",
        sequence=41,
        payload={"kind": "key", "scan_code": 30, "is_down": True},
    )

    assert FrameDecoder().feed(encode_frame(message)) == [message]
