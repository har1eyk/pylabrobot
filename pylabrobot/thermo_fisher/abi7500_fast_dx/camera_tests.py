"""Camera reconstruction tests using captured bytes and independently emulated reference output."""

import base64
import hashlib
import json
import struct
import unittest
import zlib
from pathlib import Path

from pylabrobot.thermo_fisher.abi7500_fast_dx.camera import (
  CameraFrame,
  CameraFrameAssembler,
  CameraGeometry,
)
from pylabrobot.thermo_fisher.abi7500_fast_dx.protocol import ABI7500ProtocolError

_FIXTURES = Path(__file__).parent / "test_data"
_CAPTURE = json.loads((_FIXTURES / "camera_capture_vector.json").read_text())
_CROP = json.loads((_FIXTURES / "camera_crop_vectors.json").read_text())["results"]
_CAMERA_BYTES = zlib.decompress(base64.b64decode(_CAPTURE["payload"]))


class CameraTests(unittest.TestCase):
  def test_captured_dimensions_and_exact_read_command(self) -> None:
    """The four CI bytes produce the exact CD command seen on USB."""
    geometry = CameraGeometry.decode(bytes.fromhex(_CAPTURE["ci_reply_hex"]))
    self.assertEqual((geometry.wire_width, geometry.wire_height), (321, 245))
    self.assertEqual((geometry.width, geometry.height), (320, 240))
    self.assertEqual(geometry.transfer_size, 157312)
    self.assertEqual(geometry.encode_read(), _CAPTURE["read_command"].encode("ascii"))

  def test_captured_usb_fragments_match_native_image_copy(self) -> None:
    """All pixels, including the shifted row boundary, match the native routine's output."""
    self.assertEqual(hashlib.sha256(_CAMERA_BYTES).hexdigest(), _CAPTURE["transfer_sha256"])
    assembler = CameraFrameAssembler(CameraGeometry(321, 245))
    offset = 0
    for length in _CAPTURE["usb_read_lengths"]:
      assembler.feed(_CAMERA_BYTES[offset : offset + length])
      offset += length
    self.assertEqual(assembler.remaining, 0)
    frame = assembler.finish()
    self.assertEqual(frame.wire_data, _CAMERA_BYTES)
    self.assertEqual(
      hashlib.sha256(frame.image_bytes).hexdigest(), _CAPTURE["reference_copy"]["output_sha256"]
    )
    self.assertEqual(list(frame.pixel_rows()[0][:24]), _CAPTURE["reference_copy"]["output_prefix"])
    self.assertEqual(frame.transfer_tail, _CAMERA_BYTES[-22:])

  def test_synthetic_native_vectors_in_both_copy_modes(self) -> None:
    """Native ramp vectors cover optical-black mode and unsigned 16-bit wraparound."""
    for vector in _CROP:
      with self.subTest(width=vector["width"], opb=vector["opb"]):
        geometry = CameraGeometry(vector["width"] + 1, vector["wire_height"])
        count = geometry.wire_width * geometry.wire_height
        pixels = struct.pack(f"<{count}H", *(index % 65536 for index in range(count)))
        tail = b"\xa5" * (geometry.transfer_size - len(pixels))
        frame = CameraFrame(geometry, pixels + tail, bool(vector["opb"]))
        self.assertEqual(hashlib.sha256(frame.image_bytes).hexdigest(), vector["output_sha256"])
        self.assertEqual(frame.transfer_tail, tail)
        self.assertEqual(len(frame.image_bytes), 2 * vector["output_pixels"])
        self.assertEqual(frame.pixel_rows()[0][0], vector["source_index_first"] % 65536)
        self.assertEqual(frame.pixel_rows()[-1][-1], vector["source_index_last"] % 65536)

  def test_fragmentation_at_every_small_frame_boundary(self) -> None:
    """USB reads can divide pixels and need not follow camera row boundaries."""
    geometry = CameraGeometry(5, 8)
    data = bytes(range(geometry.transfer_size))
    expected = CameraFrame(geometry, data)
    for split in range(len(data) + 1):
      with self.subTest(split=split):
        assembler = CameraFrameAssembler(geometry)
        assembler.feed(data[:split])
        assembler.feed(data[split:])
        self.assertEqual(assembler.finish(), expected)

  def test_reference_block_count_at_exact_boundary(self) -> None:
    """The native formula includes two extra blocks when pixel count is divisible by 64."""
    geometry = CameraGeometry(8, 8)
    self.assertEqual(geometry.pixel_data_size, 128)
    self.assertEqual(geometry.transfer_size, 256)
    self.assertEqual(geometry.encode_read(), b"CD  64,   4 ")

  def test_truncation_overflow_and_closed_transfer(self) -> None:
    """A failed transfer cannot be revived using a later response."""
    geometry = CameraGeometry(5, 8)
    for received in (0, 1, geometry.transfer_size - 1, geometry.transfer_size + 1):
      with self.subTest(received=received):
        assembler = CameraFrameAssembler(geometry)
        with self.assertRaises(ABI7500ProtocolError):
          assembler.feed(b"\x00" * received)
          assembler.finish()
        with self.assertRaises(ABI7500ProtocolError):
          assembler.feed(b"")
        with self.assertRaises(ABI7500ProtocolError):
          assembler.finish()
    assembler = CameraFrameAssembler(geometry)
    assembler.feed(b"\x00" * geometry.transfer_size)
    assembler.finish()
    with self.assertRaises(ABI7500ProtocolError):
      assembler.feed(b"")
    with self.assertRaises(ABI7500ProtocolError):
      assembler.finish()

  def test_invalid_geometry_and_frame_lengths(self) -> None:
    """Reject malformed dimensions and extra bytes rather than silently cropping them away."""
    for data in (b"", b"\x41\x01\xf5", b"\x41\x01\xf5\x00\x00", b"\x00" * 4):
      with self.assertRaises(ABI7500ProtocolError):
        CameraGeometry.decode(data)
    for width, height in ((1, 245), (321, 5), (-1, 245), (True, 245), (65535, 65535)):
      with self.assertRaises(ValueError):
        CameraGeometry(width, height)
    geometry = CameraGeometry(321, 245)
    for data in (_CAMERA_BYTES[:-1], _CAMERA_BYTES + b"\x00"):
      with self.assertRaises(ABI7500ProtocolError):
        CameraFrame(geometry, data)
