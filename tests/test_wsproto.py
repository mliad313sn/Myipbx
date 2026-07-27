"""The persistent bidirectional socket protocol."""

from __future__ import annotations

import base64
import os
import struct
import unittest

import support  # noqa: F401  (path setup)

from appliance import wsproto


def client_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build a masked client frame, which is what the server must accept."""
    return wsproto.build_frame(opcode, payload, fin=fin, mask=True)


class HandshakeTests(unittest.TestCase):
    def test_the_acceptance_token_matches_the_published_example(self) -> None:
        # The example key and its expected acceptance token are the ones given
        # in the protocol specification itself.
        self.assertEqual(
            wsproto.compute_accept_token("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_an_absent_key_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError):
            wsproto.compute_accept_token("")

    def test_only_a_sixteen_byte_key_is_considered_valid(self) -> None:
        good = base64.b64encode(os.urandom(16)).decode("ascii")
        short = base64.b64encode(os.urandom(8)).decode("ascii")
        self.assertTrue(wsproto.validate_client_key(good))
        self.assertFalse(wsproto.validate_client_key(short))
        self.assertFalse(wsproto.validate_client_key("not base sixty four"))


class FrameBuildingTests(unittest.TestCase):
    def test_the_three_length_encodings_are_selected_by_size(self) -> None:
        short = wsproto.build_frame(wsproto.OPCODE_TEXT, b"a" * 10)
        self.assertEqual(short[1] & 0x7F, 10)

        medium = wsproto.build_frame(wsproto.OPCODE_TEXT, b"a" * 200)
        self.assertEqual(medium[1] & 0x7F, 126)
        self.assertEqual(struct.unpack_from("!H", medium, 2)[0], 200)

        large = wsproto.build_frame(wsproto.OPCODE_TEXT, b"a" * 70000)
        self.assertEqual(large[1] & 0x7F, 127)
        self.assertEqual(struct.unpack_from("!Q", large, 2)[0], 70000)

    def test_a_server_frame_is_never_masked(self) -> None:
        frame = wsproto.build_frame(wsproto.OPCODE_TEXT, b"hello")
        self.assertEqual(frame[1] & 0x80, 0)

    def test_an_oversized_control_frame_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError):
            wsproto.build_frame(wsproto.OPCODE_PING, b"a" * 126)

    def test_a_fragmented_control_frame_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError):
            wsproto.build_frame(wsproto.OPCODE_PING, b"a", fin=False)

    def test_a_close_frame_carries_its_status_code(self) -> None:
        frame = wsproto.build_close_frame(wsproto.CLOSE_MESSAGE_TOO_BIG, "too large")
        decoder = wsproto.FrameDecoder(require_mask=False)
        events = decoder.feed(frame)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], wsproto.CloseReceived)
        self.assertEqual(events[0].code, wsproto.CLOSE_MESSAGE_TOO_BIG)
        self.assertEqual(events[0].reason, "too large")


class FrameDecodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = wsproto.FrameDecoder(maximum_message_bytes=4096)

    def test_a_masked_text_frame_is_unmasked_and_decoded(self) -> None:
        events = self.decoder.feed(client_frame(wsproto.OPCODE_TEXT, "héllo".encode("utf-8")))
        self.assertEqual(events, [wsproto.TextMessage("héllo")])

    def test_a_frame_split_across_arbitrary_chunks_is_reassembled(self) -> None:
        frame = client_frame(wsproto.OPCODE_TEXT, b"a message split at every byte")
        events: list[object] = []
        for index in range(len(frame)):
            events.extend(self.decoder.feed(frame[index : index + 1]))
        self.assertEqual(events, [wsproto.TextMessage("a message split at every byte")])

    def test_several_frames_arriving_together_are_all_produced(self) -> None:
        payload = (
            client_frame(wsproto.OPCODE_TEXT, b"one")
            + client_frame(wsproto.OPCODE_TEXT, b"two")
            + client_frame(wsproto.OPCODE_PING, b"probe")
        )
        events = self.decoder.feed(payload)
        self.assertEqual(
            events,
            [
                wsproto.TextMessage("one"),
                wsproto.TextMessage("two"),
                wsproto.PingReceived(b"probe"),
            ],
        )

    def test_a_fragmented_message_is_assembled_from_its_continuations(self) -> None:
        payload = (
            client_frame(wsproto.OPCODE_TEXT, b"first ", fin=False)
            + client_frame(wsproto.OPCODE_CONTINUATION, b"second ", fin=False)
            + client_frame(wsproto.OPCODE_CONTINUATION, b"third")
        )
        events = self.decoder.feed(payload)
        self.assertEqual(events, [wsproto.TextMessage("first second third")])

    def test_a_control_frame_may_interleave_with_a_fragmented_message(self) -> None:
        payload = (
            client_frame(wsproto.OPCODE_TEXT, b"start ", fin=False)
            + client_frame(wsproto.OPCODE_PING, b"probe")
            + client_frame(wsproto.OPCODE_CONTINUATION, b"end")
        )
        events = self.decoder.feed(payload)
        self.assertEqual(
            events, [wsproto.PingReceived(b"probe"), wsproto.TextMessage("start end")]
        )

    def test_an_unmasked_client_frame_is_a_protocol_violation(self) -> None:
        unmasked = wsproto.build_frame(wsproto.OPCODE_TEXT, b"hello", mask=False)
        with self.assertRaises(wsproto.ProtocolError) as caught:
            self.decoder.feed(unmasked)
        self.assertEqual(caught.exception.code, wsproto.CLOSE_PROTOCOL_ERROR)

    def test_a_reserved_bit_is_a_protocol_violation(self) -> None:
        frame = bytearray(client_frame(wsproto.OPCODE_TEXT, b"hello"))
        frame[0] |= 0x40
        with self.assertRaises(wsproto.ProtocolError):
            self.decoder.feed(bytes(frame))

    def test_an_undefined_opcode_is_a_protocol_violation(self) -> None:
        frame = bytearray(client_frame(wsproto.OPCODE_TEXT, b"hello"))
        frame[0] = 0x80 | 0x5
        with self.assertRaises(wsproto.ProtocolError):
            self.decoder.feed(bytes(frame))

    def test_a_continuation_with_no_open_message_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError):
            self.decoder.feed(client_frame(wsproto.OPCODE_CONTINUATION, b"orphan"))

    def test_a_new_data_frame_inside_an_open_message_is_refused(self) -> None:
        self.decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"open", fin=False))
        with self.assertRaises(wsproto.ProtocolError):
            self.decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"interrupting"))

    def test_an_oversized_message_is_refused_before_it_is_buffered(self) -> None:
        decoder = wsproto.FrameDecoder(maximum_message_bytes=1024)
        with self.assertRaises(wsproto.ProtocolError) as caught:
            decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"a" * 2048))
        self.assertEqual(caught.exception.code, wsproto.CLOSE_MESSAGE_TOO_BIG)

    def test_an_oversized_fragmented_message_is_refused(self) -> None:
        decoder = wsproto.FrameDecoder(maximum_message_bytes=1024)
        decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"a" * 600, fin=False))
        with self.assertRaises(wsproto.ProtocolError) as caught:
            decoder.feed(client_frame(wsproto.OPCODE_CONTINUATION, b"a" * 600))
        self.assertEqual(caught.exception.code, wsproto.CLOSE_MESSAGE_TOO_BIG)

    def test_invalid_character_encoding_in_a_text_message_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError) as caught:
            self.decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"\xff\xfe"))
        self.assertEqual(caught.exception.code, wsproto.CLOSE_INVALID_PAYLOAD)

    def test_a_binary_message_is_produced_without_decoding(self) -> None:
        events = self.decoder.feed(client_frame(wsproto.OPCODE_BINARY, b"\x00\x01\xff"))
        self.assertEqual(events, [wsproto.BinaryMessage(b"\x00\x01\xff")])

    def test_an_empty_close_body_means_no_status_was_supplied(self) -> None:
        events = self.decoder.feed(client_frame(wsproto.OPCODE_CLOSE, b""))
        self.assertEqual(events, [wsproto.CloseReceived(wsproto.CLOSE_NORMAL, "")])

    def test_a_truncated_close_status_is_refused(self) -> None:
        with self.assertRaises(wsproto.ProtocolError):
            self.decoder.feed(client_frame(wsproto.OPCODE_CLOSE, b"\x03"))

    def test_a_reserved_close_code_is_refused(self) -> None:
        for code in (1005, 1006, 1015, 999):
            with self.subTest(code=code):
                decoder = wsproto.FrameDecoder()
                with self.assertRaises(wsproto.ProtocolError):
                    decoder.feed(client_frame(wsproto.OPCODE_CLOSE, struct.pack("!H", code)))

    def test_nothing_is_produced_after_a_close_is_received(self) -> None:
        payload = client_frame(wsproto.OPCODE_CLOSE, struct.pack("!H", 1000))
        payload += client_frame(wsproto.OPCODE_TEXT, b"after the close")
        events = self.decoder.feed(payload)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], wsproto.CloseReceived)
        self.assertEqual(self.decoder.feed(client_frame(wsproto.OPCODE_TEXT, b"more")), [])

    def test_a_round_trip_of_every_payload_size_boundary_is_exact(self) -> None:
        for size in (0, 1, 125, 126, 127, 65535, 65536):
            with self.subTest(size=size):
                decoder = wsproto.FrameDecoder(maximum_message_bytes=131072)
                payload = os.urandom(size)
                events = decoder.feed(client_frame(wsproto.OPCODE_BINARY, payload))
                self.assertEqual(events, [wsproto.BinaryMessage(payload)])


if __name__ == "__main__":
    unittest.main()
