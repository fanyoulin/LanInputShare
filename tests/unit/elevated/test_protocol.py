from __future__ import annotations

import json
import struct

import pytest

from lan_input_share.elevated.models import HelperState
from lan_input_share.elevated.protocol import (
    HELPER_PROTOCOL_VERSION,
    MAX_HELPER_FRAME_BYTES,
    Hello,
    HelperFrameDecoder,
    HelperProtocolError,
    Input,
    Ping,
    ReleaseAll,
    ShutdownIfIdle,
    Status,
    encode_helper_message,
)
from lan_input_share.input.events import KeyEvent


@pytest.mark.parametrize(
    "message",
    [
        Hello(sequence=0, protocol_version=HELPER_PROTOCOL_VERSION, client_pid=1234),
        Input(sequence=1, event=KeyEvent(42, 0x10, True)),
        ReleaseAll(sequence=2),
        Ping(sequence=3),
        Status(sequence=4, state=HelperState.RUNNING, detail="已连接"),
        ShutdownIfIdle(sequence=5),
    ],
)
def test_helper_messages_round_trip(message: object) -> None:
    decoder = HelperFrameDecoder()

    encoded = encode_helper_message(message)  # type: ignore[arg-type]

    assert decoder.feed(encoded[:3]) == ()
    assert decoder.feed(encoded[3:]) == (message,)


def test_decoder_handles_multiple_frames_in_one_chunk() -> None:
    frames = encode_helper_message(Ping(1)) + encode_helper_message(ReleaseAll(2))

    assert HelperFrameDecoder().feed(frames) == (Ping(1), ReleaseAll(2))


@pytest.mark.parametrize(
    "mapping",
    [
        {"version": 1, "kind": "unknown", "sequence": 0, "payload": {}},
        {"version": 1, "kind": "ping", "sequence": True, "payload": {}},
        {"version": 1, "kind": "ping", "sequence": -1, "payload": {}},
        {
            "version": 1,
            "kind": "input",
            "sequence": 0,
            "payload": {"kind": "invalid"},
        },
    ],
)
def test_decoder_rejects_invalid_message(mapping: dict[str, object]) -> None:
    body = json.dumps(mapping).encode("utf-8")
    frame = struct.pack(">I", len(body)) + body

    with pytest.raises(HelperProtocolError):
        HelperFrameDecoder().feed(frame)


def test_decoder_rejects_frame_larger_than_limit() -> None:
    frame = struct.pack(">I", MAX_HELPER_FRAME_BYTES + 1)

    with pytest.raises(HelperProtocolError, match="超过限制"):
        HelperFrameDecoder().feed(frame)


def test_decoder_accepts_frame_length_equal_to_limit_before_body_arrives() -> None:
    decoder = HelperFrameDecoder()

    assert decoder.feed(struct.pack(">I", MAX_HELPER_FRAME_BYTES)) == ()


def test_decoder_is_poisoned_after_protocol_error() -> None:
    decoder = HelperFrameDecoder()
    invalid = struct.pack(">I", MAX_HELPER_FRAME_BYTES + 1)
    with pytest.raises(HelperProtocolError):
        decoder.feed(invalid)

    with pytest.raises(HelperProtocolError, match="不可继续使用"):
        decoder.feed(encode_helper_message(Ping(1)))
