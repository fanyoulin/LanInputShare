from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, cast

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

PROTOCOL_VERSION = 2


class ProtocolError(ValueError):
    """Raised when untrusted wire data violates the protocol."""


class ProtocolVersionError(ProtocolError):
    """Raised when peers use incompatible wire protocol versions."""


class MessageType(StrEnum):
    HELLO = "hello"
    AUTH_CHALLENGE = "auth_challenge"
    AUTH_RESPONSE = "auth_response"
    PAIRING_DECISION = "pairing_decision"
    INPUT = "input"
    RELEASE_INPUT = "release_input"
    HEARTBEAT = "heartbeat"
    CLIPBOARD = "clipboard"
    FILE_OFFER = "file_offer"
    FILE_REQUEST = "file_request"
    FILE_DATA = "file_data"
    FILE_ABORT = "file_abort"
    FILE_PASTE_PROBE = "file_paste_probe"
    FILE_PASTE_DECISION = "file_paste_decision"
    ROLE_CONFLICT = "role_conflict"


@dataclass(frozen=True, slots=True)
class WireMessage:
    session_id: str
    sequence: int
    payload: dict[str, JsonValue]

    TYPE: ClassVar[MessageType]

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ProtocolError("消息结构无效：session_id 不能为空")
        if self.sequence < 0:
            raise ProtocolError("消息结构无效：sequence 不能为负数")

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "version": PROTOCOL_VERSION,
            "type": self.TYPE.value,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class HelloMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.HELLO


@dataclass(frozen=True, slots=True)
class AuthChallengeMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.AUTH_CHALLENGE


@dataclass(frozen=True, slots=True)
class AuthResponseMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.AUTH_RESPONSE


@dataclass(frozen=True, slots=True)
class PairingDecisionMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.PAIRING_DECISION


@dataclass(frozen=True, slots=True)
class InputMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.INPUT


@dataclass(frozen=True, slots=True)
class ReleaseInputMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.RELEASE_INPUT


@dataclass(frozen=True, slots=True)
class HeartbeatMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.HEARTBEAT


@dataclass(frozen=True, slots=True)
class ClipboardMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.CLIPBOARD


@dataclass(frozen=True, slots=True)
class FileOfferMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_OFFER


@dataclass(frozen=True, slots=True)
class FileRequestMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_REQUEST


@dataclass(frozen=True, slots=True)
class FileDataMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_DATA


@dataclass(frozen=True, slots=True)
class FileAbortMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_ABORT


@dataclass(frozen=True, slots=True)
class FilePasteProbeMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_PASTE_PROBE


@dataclass(frozen=True, slots=True)
class FilePasteDecisionMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.FILE_PASTE_DECISION


@dataclass(frozen=True, slots=True)
class RoleConflictMessage(WireMessage):
    TYPE: ClassVar[MessageType] = MessageType.ROLE_CONFLICT


_MESSAGE_CLASSES: dict[MessageType, type[WireMessage]] = {
    cls.TYPE: cls
    for cls in (
        HelloMessage,
        AuthChallengeMessage,
        AuthResponseMessage,
        PairingDecisionMessage,
        InputMessage,
        ReleaseInputMessage,
        HeartbeatMessage,
        ClipboardMessage,
        FileOfferMessage,
        FileRequestMessage,
        FileDataMessage,
        FileAbortMessage,
        FilePasteProbeMessage,
        FilePasteDecisionMessage,
        RoleConflictMessage,
    )
}


def message_from_mapping(value: object) -> WireMessage:
    if not isinstance(value, dict):
        raise ProtocolError("消息结构无效：根节点必须是对象")
    mapping = cast(dict[object, object], value)
    try:
        version = mapping["version"]
        raw_type = mapping["type"]
        session_id = mapping["session_id"]
        sequence = mapping["sequence"]
        payload = mapping["payload"]
    except KeyError as exc:
        raise ProtocolError(f"消息结构无效：缺少 {exc.args[0]}") from exc

    if version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"协议版本不一致：本机 {PROTOCOL_VERSION}，对端 {version}，请升级两端程序"
        )
    if not isinstance(raw_type, str):
        raise ProtocolError("消息结构无效：type 必须是字符串")
    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise ProtocolError(f"未知消息类型：{raw_type}") from exc
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("消息结构无效：session_id 不能为空")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ProtocolError("消息结构无效：sequence 必须是非负整数")
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise ProtocolError("消息结构无效：payload 必须是字符串键对象")

    message_class = _MESSAGE_CLASSES[message_type]
    return message_class(
        session_id=session_id,
        sequence=sequence,
        payload=cast(dict[str, JsonValue], payload),
    )
