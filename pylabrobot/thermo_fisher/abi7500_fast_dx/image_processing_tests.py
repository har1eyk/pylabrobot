"""Test camera corrections and strict mask decoding without vendor binaries or hardware."""

import base64
import hashlib
import json
import struct
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import cast

from .acquisition import CameraAcquisition, OpticalState
from .calibration import UniformityCalibration, UniformityCorrectedData
from .camera import CameraFrame, CameraGeometry
from .image_processing import CameraWellData, OpticalBlackImage, PlatePixelMask
from .protocol import ABI7500ProtocolError
from .results import SavedFilterData

_DATA = Path(__file__).parent / "test_data"
_ORACLE = json.loads((_DATA / "optical_black_vectors.json").read_text())
_CAPTURE = json.loads((_DATA / "camera_capture_vector.json").read_text())
_UBUNTU = json.loads((_DATA / "ubuntu_optics_vector.json").read_text())


def _new_class(name: bytes, schema: int) -> bytes:
  """Encode one MFC class header for a synthetic archive."""
  return struct.pack("<HHH", 0xFFFF, schema, len(name)) + name


def _mask() -> bytes:
  """Build 96 distinct, asymmetric synthetic regions; no instrument calibration is embedded."""
  data = bytearray(_new_class(b"PlateMask", 10) + struct.pack("<H", 96))
  for index in range(96):
    row, col = 10 + (index // 12) * 25, 10 + (index % 12) * 25
    data.extend(_new_class(b"CMNImageAOI", 2) if index == 0 else b"\x03\x80")
    data.extend(struct.pack("<ddddIdd", col + 1, row + 0.5, 3, 2, 0, col + 1, row + 0.5))
    data.extend(bytes(94))
    data.extend(struct.pack("<HHiiii", 2, 2, row, col, row + 1, col + 2))
    data.extend(_new_class(b"CMNImageMatrix", 1) if index == 0 else b"\x05\x80")
    data.extend(struct.pack("<IIIIHiiHiHHHHii", 2, 2, 3, 3, 2, 0, 1, 0, 0, 0, 0, 255, 0, 0, 0))
    data.extend(struct.pack("<6H", 1, 0, 255, 0, 2, 0))
  return bytes(data) + bytes(30)


def _opb_frame(pixels: list[int]) -> CameraFrame:
  """Place synthetic OPB-view pixels in a wire transfer with distinct unused margin values."""
  wire = [43210] * (321 * 245)
  for row in range(245):
    wire[row * 321 : row * 321 + 320] = pixels[row * 320 : (row + 1) * 320]
  return CameraFrame(CameraGeometry(321, 245), struct.pack("<78645H", *wire) + bytes(22))


def _acquisition() -> CameraAcquisition:
  """Reconstruct the saved Ubuntu acquisition from the compressed transport fixture."""
  operations = json.loads(zlib.decompress(base64.b64decode(_UBUNTU["operations"])))
  start = next(i for i, op in enumerate(operations) if op[:2] == ["write", b"CD  64,2458 ".hex()])
  image = b"".join(bytes.fromhex(op[1]) for op in operations[start + 1 : start + 40])
  return CameraAcquisition.from_dict(dict(_UBUNTU["checkpoint"], wire_data=image.hex()))


class ImageProcessingTests(unittest.TestCase):
  """Preserve exact camera data and prevent interpreting diagnostic sums as calibrated signals."""

  def test_native_optical_black_capture(self) -> None:
    """All captured output pixels match the independently executed HAMCamera routine."""
    frame = CameraFrame(
      CameraGeometry(321, 245), zlib.decompress(base64.b64decode(_CAPTURE["payload"]))
    )
    corrected = OpticalBlackImage(frame)
    expected = _ORACLE["reference_capture"]
    self.assertEqual(corrected.offset, expected["optical_black_offset"])
    self.assertEqual(
      hashlib.sha256(corrected.image_bytes).hexdigest(), expected["corrected_sha256"]
    )
    self.assertIs(corrected.source, frame)
    self.assertEqual(hashlib.sha256(frame.wire_data).hexdigest(), expected["input_sha256"])

  def test_native_rounding_clipping_overflow_and_copy_origin(self) -> None:
    """Compare fractional means and uint16 boundaries to original native execution."""
    for vector in _ORACLE["synthetic_cases"]:
      pixels = [vector["black"]] * 78400
      pixels[1] = vector["first_black_adjusted"]
      pixels[1600:1608] = vector["input_pixels"]
      frame = _opb_frame(pixels)
      corrected = OpticalBlackImage(frame)
      self.assertEqual(hashlib.sha256(corrected.image_bytes).hexdigest(), vector["sha256"])
      self.assertEqual(
        list(struct.unpack_from("<8H", corrected.image_bytes)), vector["output_pixels"]
      )
      # Selecting a view must not change correction, which always uses the original OPB=1 copy.
      self.assertEqual(
        OpticalBlackImage(replace(frame, include_optical_black=True)).image_bytes,
        corrected.image_bytes,
      )
    with self.assertRaises(ValueError):
      OpticalBlackImage(CameraFrame(CameraGeometry(2, 6), bytes(128)))

  def test_mask_order_coordinates_and_nonbinary_weights(self) -> None:
    """The mask selects every nonzero matrix entry once, including values other than one."""
    source = _mask()
    mask = PlatePixelMask(source, filter=1)
    self.assertEqual(len(mask.wells), 96)
    self.assertEqual(mask.wells[0], ((10, 10), (10, 12), (11, 11)))
    self.assertEqual(mask.wells[-1], ((185, 285), (185, 287), (186, 286)))
    self.assertEqual(mask.sha256, hashlib.sha256(source).hexdigest())
    self.assertEqual(mask.source_bytes, source)

  def test_reject_truncated_unknown_and_oversized_masks(self) -> None:
    """Malformed archives cannot silently substitute a grid or a partly decoded plate."""
    source = _mask()
    for length in (0, 2, 10, 30, 240, 1000, len(source) - 1):
      with self.subTest(length=length), self.assertRaises(ABI7500ProtocolError):
        PlatePixelMask(source[:length], filter=1)
    for invalid in (
      source + b"\x00",
      source.replace(b"PlateMask", b"OtherMask", 1),
      source.replace(b"\xff\xff\x0a\x00", b"\xff\xff\x09\x00", 1),
      source.replace(b"\x03\x80", b"\xff\x7f", 1),
      source.replace(
        struct.pack("<HHiiii", 2, 2, 10, 10, 11, 12),
        struct.pack("<HHiiii", 2, 2, -1, 10, 11, 12),
        1,
      ),
      source.replace(
        struct.pack("<IIIIHii", 2, 2, 3, 3, 2, 0, 1),
        struct.pack("<IIIIHii", 2, 2, 3, 3, 2, 1, 0),
        1,
      ),
      source.replace(struct.pack("<6H", 1, 0, 255, 0, 2, 0), bytes(12), 1),
    ):
      with self.assertRaises(ABI7500ProtocolError):
        PlatePixelMask(invalid, filter=1)
    for filter in (0, 6, True):
      with self.assertRaises(ValueError):
        PlatePixelMask(source, filter=filter)
    with self.assertRaises(ValueError):
      PlatePixelMask(bytes(1024 * 1024 + 1), filter=1)

  def test_pixel_statistics_and_provenance(self) -> None:
    """Mask sums retain acquisition identity and distinguish corrected and untouched images."""
    acquisition = _acquisition()
    self.assertEqual(hashlib.sha256(acquisition.wire_data).hexdigest(), _UBUNTU["wire_sha256"])
    source = replace(acquisition, wire_data=_opb_frame([100] * 78400).wire_data)
    mask = PlatePixelMask(_mask(), filter=1)
    data = CameraWellData(source, mask, correction="optical_black")
    self.assertIs(data.source, source)
    self.assertIs(data.mask, mask)
    self.assertEqual([m.label for m in data.measurements][::12], [f"{r}1" for r in "ABCDEFGH"])
    self.assertEqual(data.measurements[-1].label, "H12")
    for measurement in data.measurements:
      self.assertEqual(
        (measurement.pixel_count, measurement.pixel_sum, measurement.mean), (3, 384, 128)
      )
      self.assertEqual((measurement.minimum, measurement.maximum), (128, 128))
    raw = CameraWellData(source, mask, correction="none")
    self.assertEqual(raw.measurements[0].pixel_sum, 300)
    with self.assertRaisesRegex(TypeError, "saved vendor"):
      UniformityCorrectedData(cast(SavedFilterData, data), cast(UniformityCalibration, None))

  def test_incomplete_or_mismatched_acquisition_is_rejected(self) -> None:
    """Partial frames, a different filter, and moving optics cannot produce well results."""
    source = _acquisition()
    mask = PlatePixelMask(_mask(), filter=1)
    with self.assertRaisesRegex(ValueError, "filter"):
      CameraWellData(source, PlatePixelMask(_mask(), filter=2), correction="none")
    with self.assertRaisesRegex(ValueError, "complete"):
      CameraWellData(replace(source, state="uncertain"), mask, correction="none")
    changed = OpticalState.from_dict(dict(source.optics_before.to_dict(), filter="0002"))
    with self.assertRaisesRegex(ValueError, "changed"):
      CameraWellData(replace(source, optics_after=changed), mask, correction="none")
