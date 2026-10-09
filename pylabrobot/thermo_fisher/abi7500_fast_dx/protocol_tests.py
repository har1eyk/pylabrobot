"""Offline codec tests; importing or running this module never opens hardware."""

import json
import unittest
from pathlib import Path

from pylabrobot.thermo_fisher.abi7500_fast_dx.protocol import (
  ABI7500ProtocolError,
  ThermalPacket,
  ThermalPacketDecoder,
  decode_serial_read,
  encode_serial_read,
  encode_serial_write,
)

_VECTORS = json.loads(
  (Path(__file__).parent / "test_data" / "encoder_vectors.json").read_text(encoding="utf-8")
)["vectors"]
_LOG_VECTORS = json.loads(
  (Path(__file__).parent / "test_data" / "thermal_log_vectors.json").read_text(encoding="utf-8")
)["vectors"]
_USB_VECTORS = json.loads(
  (Path(__file__).parent / "test_data" / "usb_serial_vectors.json").read_text(encoding="utf-8")
)
_QUERY = bytes.fromhex("5858ffffffff000f393730300006000000000001e6")


class ThermalPacketTests(unittest.TestCase):
  def test_captured_run_frames_and_usb_write_boundaries(self) -> None:
    """A real program spans two USB writes; start/stop fixtures retain exact sequence bytes."""
    fixture = json.loads((Path(__file__).parent / "test_data" / "usb_run_vectors.json").read_text())
    for vector in fixture["vectors"]:
      with self.subTest(session=vector["session"], command=vector["command"]):
        packet = ThermalPacket(
          vector["command"], vector["sequence"], bytes.fromhex(vector["payload_hex"])
        )
        self.assertEqual(packet.encode(), bytes.fromhex(vector["request_packet_hex"]))
        self.assertEqual(
          encode_serial_write(packet.encode()),
          tuple(bytes.fromhex(chunk) for chunk in vector["usb_write_hex"]),
        )
        reply = ThermalPacket.decode(bytes.fromhex(vector["reply_packet_hex"]))
        self.assertEqual((reply.command, reply.sequence), (packet.command, packet.sequence))
        self.assertEqual(reply.error_code, 0)

  def test_matches_vendor_log_requests_and_replies(self) -> None:
    for vector in _LOG_VECTORS:
      with self.subTest(vector=vector["name"]):
        wire = bytes.fromhex(vector["packet_hex"])
        packet = ThermalPacket.decode(wire)
        self.assertEqual(packet.command, vector["command"])
        self.assertEqual(packet.sequence, vector["sequence"])
        self.assertEqual(packet.payload, bytes.fromhex(vector["payload_hex"]))
        self.assertEqual(packet.error_code, 0)
        self.assertEqual(packet.address, b"\xff" * 4)
        self.assertEqual(packet.encode(), wire)

  def test_matches_reference_binary_encoder(self) -> None:
    for vector in _VECTORS:
      with self.subTest(vector=vector["name"]):
        expected = bytes.fromhex(vector["packet_hex"])
        packet = ThermalPacket(
          command=vector["command"],
          sequence=vector["sequence"],
          payload=bytes.fromhex(vector["payload_hex"]),
        )
        self.assertEqual(packet.encode(), expected)
        self.assertEqual(ThermalPacket.decode(expected), packet)

  def test_error_and_address_are_preserved_without_interpretation(self) -> None:
    data = bytes.fromhex("585812345678000f393730300006000700000001ed")
    packet = ThermalPacket.decode(data)
    self.assertEqual(packet.error_code, 7)
    self.assertEqual(packet.address, bytes.fromhex("12345678"))
    self.assertEqual(packet.encode(), data)

  def test_rejects_corrupt_header_and_checksum(self) -> None:
    for index in (0, 1, 6, 7, 8, 12, 14, 16, 20):
      with self.subTest(index=index):
        corrupted = bytearray(_QUERY)
        corrupted[index] ^= 1
        with self.assertRaises(ABI7500ProtocolError):
          ThermalPacket.decode(bytes(corrupted))

  def test_rejects_truncation_and_extra_bytes(self) -> None:
    for length in range(len(_QUERY)):
      with self.subTest(length=length):
        with self.assertRaises(ABI7500ProtocolError):
          ThermalPacket.decode(_QUERY[:length])
    for suffix in (b"#", b"\x00", _QUERY):
      with self.assertRaises(ABI7500ProtocolError):
        ThermalPacket.decode(_QUERY + suffix)

  def test_rejects_unrepresentable_fields(self) -> None:
    for command in (-1, 65536, True):
      with self.assertRaises(ValueError):
        ThermalPacket(command, 1)
    for sequence in (-1, 0x100000000, True):
      with self.assertRaises(ValueError):
        ThermalPacket(6, sequence)
    for error in (-1, 65536, True):
      with self.assertRaises(ValueError):
        ThermalPacket(6, 1, error_code=error)
    for address in (b"", b"123", b"12345"):
      with self.assertRaises(ValueError):
        ThermalPacket(6, 1, address=address)
    with self.assertRaises(ValueError):
      ThermalPacket(6, 1, payload=b"\x00" * 1004)


class ThermalPacketDecoderTests(unittest.TestCase):
  def test_fragmentation_at_every_byte_boundary(self) -> None:
    for vector in [*_VECTORS, *_LOG_VECTORS]:
      data = bytes.fromhex(vector["packet_hex"])
      expected = ThermalPacket.decode(data)
      for boundary in range(1, len(data)):
        with self.subTest(vector=vector["name"], boundary=boundary):
          decoder = ThermalPacketDecoder()
          self.assertEqual(decoder.feed(data[:boundary]), [])
          self.assertEqual(decoder.feed(data[boundary:]), [expected])
          decoder.finish()

  def test_coalesced_packets_followed_by_partial_packet(self) -> None:
    decoder = ThermalPacketDecoder()
    packet = ThermalPacket.decode(_QUERY)
    self.assertEqual(decoder.feed(_QUERY * 2 + _QUERY[:10]), [packet, packet])
    self.assertEqual(decoder.feed(_QUERY[10:]), [packet])
    self.assertEqual(decoder.feed(b""), [])
    decoder.finish()

  def test_packet_markers_and_nulls_inside_payload(self) -> None:
    packet = ThermalPacket(33, 17, b"XX\x00\xff9700#" + _QUERY)
    decoder = ThermalPacketDecoder()
    decoded = []
    for byte in packet.encode():
      decoded.extend(decoder.feed(bytes([byte])))
    self.assertEqual(decoded, [packet])
    decoder.finish()

  def test_invalid_length_fails_before_waiting_for_payload(self) -> None:
    for length in (0, 14, 1019, 65535):
      decoder = ThermalPacketDecoder()
      with self.assertRaisesRegex(ABI7500ProtocolError, "Invalid thermal packet size"):
        decoder.feed(b"XX\xff\xff\xff\xff" + length.to_bytes(2, "big"))

  def test_corruption_requires_explicit_reset(self) -> None:
    decoder = ThermalPacketDecoder()
    with self.assertRaisesRegex(ABI7500ProtocolError, "checksum"):
      decoder.feed(_QUERY[:-1] + b"\x00" + _QUERY)
    with self.assertRaisesRegex(ABI7500ProtocolError, "reset"):
      decoder.feed(_QUERY)
    with self.assertRaisesRegex(ABI7500ProtocolError, "reset"):
      decoder.finish()
    decoder.reset()
    self.assertEqual(decoder.feed(_QUERY), [ThermalPacket.decode(_QUERY)])
    decoder.finish()

  def test_finish_detects_every_truncation(self) -> None:
    for length in range(1, len(_QUERY)):
      decoder = ThermalPacketDecoder()
      decoder.feed(_QUERY[:length])
      with self.assertRaisesRegex(ABI7500ProtocolError, "Truncated thermal stream"):
        decoder.finish()
      with self.assertRaisesRegex(ABI7500ProtocolError, "reset"):
        decoder.feed(_QUERY)


class SerialTunnelTests(unittest.TestCase):
  def test_captured_writes_and_fragmented_padded_replies(self) -> None:
    for exchange in _USB_VECTORS["exchanges"]:
      with self.subTest(exchange=exchange["name"]):
        request = bytes.fromhex(exchange["thermal_request_hex"])
        self.assertEqual(encode_serial_write(request), (bytes.fromhex(exchange["write_hex"]),))
        decoder = ThermalPacketDecoder()
        reads = exchange["serial_reads"]
        for index, read in enumerate(reads):
          size = read["requested_size"]
          self.assertEqual(encode_serial_read(size), bytes.fromhex(read["request_hex"]))
          serial = decode_serial_read(bytes.fromhex(read["reply_hex"]), requested_size=size)
          self.assertEqual(len(serial), read["valid_count"])
          packets = decoder.feed(serial)
          if index < len(reads) - 1:
            self.assertEqual(packets, [])
          else:
            self.assertEqual(
              [packet.encode() for packet in packets],
              [bytes.fromhex(exchange["thermal_reply_hex"])],
            )
        decoder.finish()

  def test_captured_empty_read_discards_nonzero_padding(self) -> None:
    for read in _USB_VECTORS["empty_reads"]:
      reply = bytes.fromhex(read["reply_hex"])
      self.assertTrue(any(reply[2:]))
      self.assertEqual(decode_serial_read(reply, requested_size=read["requested_size"]), b"")
      with self.assertRaises(ABI7500ProtocolError):
        decode_serial_read(reply)

  def test_padding_cannot_introduce_a_thermal_packet(self) -> None:
    reply = b"\x00\x00" + _QUERY
    serial = decode_serial_read(reply, requested_size=len(_QUERY))
    decoder = ThermalPacketDecoder()
    self.assertEqual(decoder.feed(serial), [])
    decoder.finish()

  def test_requested_capacity_rejects_bad_counts_and_wire_lengths(self) -> None:
    with self.assertRaisesRegex(ABI7500ProtocolError, "exceeds requested capacity"):
      decode_serial_read(b"\x03\x00ab", requested_size=2)
    for reply in (b"\x01\x00a", b"\x01\x00abc"):
      with self.assertRaisesRegex(ABI7500ProtocolError, "Expected 4"):
        decode_serial_read(reply, requested_size=2)
    for size in (-1, 1025, True):
      with self.assertRaises(ValueError):
        decode_serial_read(b"\x00\x00", requested_size=size)
    self.assertEqual(decode_serial_read(b"\x00\x00", requested_size=0), b"")
    self.assertEqual(decode_serial_read(b"\x01\x00a" + b"\xff" * 1023, requested_size=1024), b"a")

  def test_binary_write_header(self) -> None:
    self.assertEqual(encode_serial_write(b"\x00\xff#"), (b"RS   3\x00\xff#",))
    self.assertEqual(encode_serial_write(b""), (b"RS   0",))
    self.assertEqual(encode_serial_write(_QUERY), (b"RS  21" + _QUERY,))

  def test_write_chunk_boundaries_and_limit(self) -> None:
    self.assertEqual(encode_serial_write(b"a" * 58), (b"RS  58" + b"a" * 58,))
    self.assertEqual(encode_serial_write(b"a" * 59), (b"RS  59" + b"a" * 58, b"a"))
    maximum = b"\x00\xff" * 512
    chunks = encode_serial_write(maximum)
    self.assertEqual([len(chunk) for chunk in chunks], [64] * 16 + [6])
    self.assertEqual(b"".join(chunks), b"RS1024" + maximum)
    with self.assertRaises(ValueError):
      encode_serial_write(b"a" * 1025)

  def test_read_request_length_includes_count(self) -> None:
    self.assertEqual(encode_serial_read(0), b"RR   2")
    self.assertEqual(encode_serial_read(21), b"RR  23")
    self.assertEqual(encode_serial_read(1024), b"RR1026")
    for size in (-1, 1025, True):
      with self.assertRaises(ValueError):
        encode_serial_read(size)

  def test_read_reply_little_endian_count_and_binary_data(self) -> None:
    self.assertEqual(decode_serial_read(b"\x00\x00"), b"")
    self.assertEqual(decode_serial_read(b"\x15\x00" + _QUERY), _QUERY)
    self.assertEqual(decode_serial_read(b"\x00\x01" + bytes(range(256))), bytes(range(256)))
    self.assertEqual(decode_serial_read(b"\x00\x04" + b"\xff" * 1024), b"\xff" * 1024)

  def test_read_reply_rejects_truncation_padding_and_oversize(self) -> None:
    for data in (b"", b"\x00", b"\x01\x00", b"\x00\x00\x00", b"\x01\x04" + b"a" * 1025):
      with self.subTest(data=data[:10]):
        with self.assertRaises(ABI7500ProtocolError):
          decode_serial_read(data)
