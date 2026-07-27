"""Persistent bidirectional socket protocol.

A direct implementation of the standard socket upgrade handshake and frame
format.  It is deliberately free of input and output so that every branch —
fragmentation, masking, oversized payloads, malformed control frames — is
reachable from a unit test without opening a socket.  The transport lives in
``wsserver.py`` and consumes this module.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "OPCODE_CONTINUATION", "OPCODE_TEXT", "OPCODE_BINARY",
    "OPCODE_CLOSE", "OPCODE_PING", "OPCODE_PONG",
    "CLOSE_NORMAL", "CLOSE_GOING_AWAY", "CLOSE_PROTOCOL_ERROR",
    "CLOSE_UNSUPPORTED", "CLOSE_INVALID_PAYLOAD", "CLOSE_POLICY_VIOLATION",
    "CLOSE_MESSAGE_TOO_BIG", "CLOSE_INTERNAL_ERROR",
    "ProtocolError", "TextMessage", "BinaryMessage",
    "PingReceived", "PongReceived", "CloseReceived",
    "compute_accept_token", "build_frame", "build_close_frame",
    "FrameDecoder", "HANDSHAKE_GUID",
]

# The fixed identifier the standard requires be concatenated with the client
# supplied key before hashing.
HANDSHAKE_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

_CONTROL_OPCODES = frozenset({OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG})
_DATA_OPCODES = frozenset({OPCODE_TEXT, OPCODE_BINARY})
_KNOWN_OPCODES = _CONTROL_OPCODES | _DATA_OPCODES | {OPCODE_CONTINUATION}

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_INVALID_PAYLOAD = 1007
CLOSE_POLICY_VIOLATION = 1008
CLOSE_MESSAGE_TOO_BIG = 1009
CLOSE_INTERNAL_ERROR = 1011

#: Close codes a peer is never permitted to send on the wire.
_RESERVED_CLOSE_CODES = frozenset({1004, 1005, 1006, 1015})

_MAXIMUM_CONTROL_PAYLOAD = 125
_MASK_BYTES = 4


class ProtocolError(Exception):
    """A violation of the socket protocol, carrying the close code to send."""

    def __init__(self, message: str, code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TextMessage:
    text: str


@dataclass(frozen=True)
class BinaryMessage:
    data: bytes


@dataclass(frozen=True)
class PingReceived:
    payload: bytes


@dataclass(frozen=True)
class PongReceived:
    payload: bytes


@dataclass(frozen=True)
class CloseReceived:
    code: int
    reason: str


def compute_accept_token(client_key: str) -> str:
    """Derive the handshake acceptance token from the client supplied key."""
    if not client_key:
        raise ProtocolError("the upgrade request carried no key")
    digest = hashlib.sha1((client_key.strip() + HANDSHAKE_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_client_key(client_key: str) -> bool:
    """Report whether a supplied key is a plausible sixteen byte value."""
    try:
        return len(base64.b64decode(client_key.strip(), validate=True)) == 16
    except (ValueError, TypeError):
        return False


def build_frame(
    opcode: int,
    payload: bytes = b"",
    fin: bool = True,
    mask: bool = False,
) -> bytes:
    """Serialise one frame.

    Server to client frames are unmasked, which is the default here.  The
    masking path exists so that the test suite can synthesise client frames
    without a second implementation drifting from this one.
    """
    if opcode not in _KNOWN_OPCODES:
        raise ProtocolError(f"the opcode {opcode} is not defined by the protocol")
    if opcode in _CONTROL_OPCODES:
        if len(payload) > _MAXIMUM_CONTROL_PAYLOAD:
            raise ProtocolError("a control frame payload exceeds the permitted size")
        if not fin:
            raise ProtocolError("a control frame may not be fragmented")

    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)

    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))

    if not mask:
        return bytes(header) + payload

    masking_key = os.urandom(_MASK_BYTES)
    header.extend(masking_key)
    return bytes(header) + _apply_mask(payload, masking_key)


def build_close_frame(code: int = CLOSE_NORMAL, reason: str = "") -> bytes:
    """Serialise a close frame with a status code and a reason."""
    encoded_reason = reason.encode("utf-8")
    # The reason must fit alongside the two byte status code.
    encoded_reason = encoded_reason[: _MAXIMUM_CONTROL_PAYLOAD - 2]
    return build_frame(OPCODE_CLOSE, struct.pack("!H", code) + encoded_reason)


def _apply_mask(payload: bytes, masking_key: bytes) -> bytes:
    if len(masking_key) != _MASK_BYTES:
        raise ProtocolError("a masking key must be four bytes")
    # Repeating the key to the payload length and translating in one pass is
    # markedly faster than a per byte loop on the older processors this
    # appliance targets.
    if not payload:
        return b""
    repeated = masking_key * (len(payload) // _MASK_BYTES + 1)
    return bytes(
        byte ^ key for byte, key in zip(payload, repeated[: len(payload)])
    )


class FrameDecoder:
    """Incremental frame decoder.

    Bytes are fed in arbitrary chunks; complete protocol events are returned.
    The decoder enforces the peer's obligations — masking, reserved bits,
    control frame limits, fragmentation ordering, message size ceiling, and
    text validity — and raises ``ProtocolError`` carrying the close code the
    transport should send.
    """

    def __init__(
        self,
        maximum_message_bytes: int = 262144,
        require_mask: bool = True,
    ) -> None:
        if maximum_message_bytes < _MAXIMUM_CONTROL_PAYLOAD:
            raise ValueError("the message ceiling cannot be below a control frame")
        self.maximum_message_bytes = maximum_message_bytes
        self.require_mask = require_mask

        self._buffer = bytearray()
        self._fragments = bytearray()
        self._fragment_opcode: int | None = None
        self._closed = False

    def feed(self, data: bytes) -> list[object]:
        """Consume bytes and return every complete event they produced."""
        if self._closed:
            return []
        self._buffer.extend(data)

        events: list[object] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                break
            produced = self._handle_frame(*frame)
            if produced is not None:
                events.append(produced)
            if isinstance(produced, CloseReceived):
                self._closed = True
                break
        return events

    # -- framing -----------------------------------------------------------

    def _take_frame(self) -> tuple[bool, int, bytes] | None:
        """Remove one complete frame from the buffer, or report insufficiency."""
        buffer = self._buffer
        if len(buffer) < 2:
            return None

        first, second = buffer[0], buffer[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise ProtocolError("a reserved bit was set without a negotiated extension")
        opcode = first & 0x0F
        if opcode not in _KNOWN_OPCODES:
            raise ProtocolError(f"the opcode {opcode} is not defined by the protocol")

        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2

        if opcode in _CONTROL_OPCODES:
            if length > _MAXIMUM_CONTROL_PAYLOAD:
                raise ProtocolError("a control frame payload exceeds the permitted size")
            if not fin:
                raise ProtocolError("a control frame may not be fragmented")

        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = struct.unpack_from("!H", buffer, offset)[0]
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = struct.unpack_from("!Q", buffer, offset)[0]
            offset += 8
            if length >> 63:
                raise ProtocolError("the declared payload length is not valid")

        if self.require_mask and not masked:
            raise ProtocolError("every client frame must be masked")

        # Reject an oversized payload before reserving memory for it, rather
        # than buffering it and discovering the problem afterwards.
        prospective = length + (
            len(self._fragments) if opcode == OPCODE_CONTINUATION or not fin else 0
        )
        if prospective > self.maximum_message_bytes:
            raise ProtocolError(
                "the message exceeds the permitted size", CLOSE_MESSAGE_TOO_BIG
            )

        masking_key = b""
        if masked:
            if len(buffer) < offset + _MASK_BYTES:
                return None
            masking_key = bytes(buffer[offset : offset + _MASK_BYTES])
            offset += _MASK_BYTES

        if len(buffer) < offset + length:
            return None

        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]

        if masked:
            payload = _apply_mask(payload, masking_key)
        return fin, opcode, payload

    # -- semantics ---------------------------------------------------------

    def _handle_frame(self, fin: bool, opcode: int, payload: bytes) -> object | None:
        if opcode == OPCODE_PING:
            return PingReceived(payload)
        if opcode == OPCODE_PONG:
            return PongReceived(payload)
        if opcode == OPCODE_CLOSE:
            return self._decode_close(payload)

        if opcode in _DATA_OPCODES:
            if self._fragment_opcode is not None:
                raise ProtocolError(
                    "a new data frame arrived while a fragmented message was open"
                )
            if fin:
                return self._complete(opcode, payload)
            self._fragment_opcode = opcode
            self._fragments.extend(payload)
            return None

        # Continuation.
        if self._fragment_opcode is None:
            raise ProtocolError("a continuation frame arrived with no message open")
        self._fragments.extend(payload)
        if len(self._fragments) > self.maximum_message_bytes:
            raise ProtocolError(
                "the message exceeds the permitted size", CLOSE_MESSAGE_TOO_BIG
            )
        if not fin:
            return None

        opcode_of_message = self._fragment_opcode
        assembled = bytes(self._fragments)
        self._fragments.clear()
        self._fragment_opcode = None
        return self._complete(opcode_of_message, assembled)

    @staticmethod
    def _complete(opcode: int, payload: bytes) -> object:
        if opcode == OPCODE_TEXT:
            try:
                return TextMessage(payload.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ProtocolError(
                    "a text message was not valid character encoding",
                    CLOSE_INVALID_PAYLOAD,
                ) from error
        return BinaryMessage(payload)

    @staticmethod
    def _decode_close(payload: bytes) -> CloseReceived:
        if not payload:
            # An empty close body means "no status was supplied", which the
            # standard represents with a code the wire never carries.
            return CloseReceived(CLOSE_NORMAL, "")
        if len(payload) == 1:
            raise ProtocolError("a close frame carried a truncated status code")

        code = struct.unpack_from("!H", payload, 0)[0]
        if code in _RESERVED_CLOSE_CODES or code < 1000 or (1016 <= code <= 2999):
            raise ProtocolError(f"the close code {code} may not be sent on the wire")
        try:
            reason = payload[2:].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError(
                "a close reason was not valid character encoding", CLOSE_INVALID_PAYLOAD
            ) from error
        return CloseReceived(code, reason)


def iterate_frames(data: bytes, **options: object) -> Iterable[object]:
    """Decode a complete byte string in one call.  Used by the test suite."""
    decoder = FrameDecoder(**options)  # type: ignore[arg-type]
    return decoder.feed(data)
