"""Replay small recorded image patches through the complete supported fluorescence pipeline."""

import base64
import json
import struct
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .acquisition import CameraExposure
from .calibration import UniformityCalibration
from .camera import CameraFrame, CameraGeometry
from .fluorescence import BackgroundCalibration, CameraFilterResult, FluorescenceFrame
from .image_processing import CameraWellData, PlatePixelMask
from .image_processing_tests import _acquisition, _new_class

_VECTORS = json.loads((Path(__file__).parent / "test_data/fluorescence_vectors.json").read_text())[
  "vectors"
]


def _background(offset: float, slope: float) -> BackgroundCalibration:
  """Repeat a selected coefficient in a synthetic native-format 96-well calibration."""
  text = (
    "Background Calibration, version = 5\n[Offsets] rows = Filters A B C D E: columns = wells\n"
  )
  text += ((f"{offset}," * 96) + "\n") * 5
  text += (
    "[slopes], from formula for water subtraction [(Dye-offset)*VE/exp] - "
    "[(slope*VE)]: Rows = Filters A B C D E: columns = wells\n"
  )
  text += ((f"{slope}," * 96) + "\n") * 5
  return BackgroundCalibration(text.encode())


def _uniformity(factor: float) -> UniformityCalibration:
  """Build a format-valid synthetic calibration, preserving a selected binary32 factor."""
  text = (
    "[head]\nversion=1.0.0\ntimestamp=0\n[instrument]\nid=test\ntype=sds7500fast\n"
    "[scanner]\nid=test\nfilters=1,2,3,4,5\n[block]\ntype=96fast\nrows=8\ncolumns=12\n"
    "[result]\nresult=true\n[uniformity]\n"
  )
  for f in range(1, 6):
    text += f"{f}=<<eof\n" + (",".join([str(factor)] * 12) + "\n") * 8 + "eof\n"
  return UniformityCalibration(text.encode())


def _mask(vector: dict[str, Any]) -> PlatePixelMask:
  """Repeat a single reference region in an explicitly synthetic 96-region archive."""
  points = set(map(tuple, vector["mask_pixels"]))
  r0, c0 = min(r for r, c in points), min(c for r, c in points)
  r1, c1 = max(r for r, c in points), max(c for r, c in points)
  rows, cols = r1 - r0 + 1, c1 - c0 + 1
  data = bytearray(_new_class(b"PlateMask", 10) + struct.pack("<H", 96))
  for i in range(96):
    data += _new_class(b"CMNImageAOI", 2) if i == 0 else b"\x03\x80"
    data += struct.pack("<ddddIdd", 1, 1, cols, rows, 0, 1, 1) + bytes(94)
    data += struct.pack("<HHiiii", 2, 2, r0, c0, r1, c1)
    data += _new_class(b"CMNImageMatrix", 1) if i == 0 else b"\x05\x80"
    data += struct.pack(
      "<IIIIHiiHiHHHHii", rows, rows, cols, cols, 2, 0, 1, 0, 0, 0, 0, 255, 0, 0, 0
    )
    data += struct.pack(
      f"<{rows * cols}H",
      *(int((r, c) in points) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)),
    )
  return PlatePixelMask(bytes(data) + bytes(30), filter=vector["physical_filter"])


def _frames(
  vector: dict[str, Any],
) -> tuple[FluorescenceFrame, FluorescenceFrame, FluorescenceFrame]:
  """Embed recorded patches into raw camera buffers, preserving their asymmetric orientation."""
  mask = _mask(vector)
  r0, c0, r1, c1 = vector["bounds"]
  result = []
  for patch, exposure in zip(vector["patches"], vector["exposures"]):
    pixels = [128] * 76800
    values = iter(
      struct.unpack(f"<{(r1 - r0 + 1) * (c1 - c0 + 1)}H", zlib.decompress(base64.b64decode(patch)))
    )
    for r in range(r0, r1 + 1):
      for c in range(c0, c1 + 1):
        pixels[r * 320 + c] = next(values)
    pixels.reverse()
    wire = [128] * 78645
    for r in range(240):
      start = 321 * (r + 5) - 1
      wire[start : start + 320] = pixels[r * 320 : (r + 1) * 320]
    frame = CameraFrame(CameraGeometry(321, 245), struct.pack("<78645H", *wire) + bytes(22))
    result.append(FluorescenceFrame(frame, CameraExposure(exposure), mask))
  return result[0], result[1], result[2]


class FluorescenceTests(unittest.TestCase):
  """No vendor binary or instrument is needed to check recorded numerical results."""

  def test_recorded_vendor_results_and_provenance(self) -> None:
    """Ten patches cover both cycles, all filters, coefficient precision and saturated fallback."""
    for vector in _VECTORS:
      with self.subTest(filter=vector["physical_filter"], cycle=vector["cycle"]):
        frames = _frames(vector)
        background = _background(vector["background_offset"], vector["background_slope"])
        uniformity = _uniformity(vector["uniformity_factor"])
        result = CameraFilterResult(frames, background, uniformity, 2, vector["cycle"], 1)
        self.assertIs(result.frames, frames)
        self.assertIs(result.background, background)
        self.assertEqual(result.filter, vector["saved_filter"])
        for value in result.measurements:
          self.assertEqual(value.raw_value, vector["raw_value"])
          self.assertEqual(struct.pack(">f", value.corrected_value).hex(), vector["corrected_bits"])
          self.assertEqual(value.selection.selected_index, vector["selected_index"])
          self.assertEqual(value.selection.saturated, tuple(vector["saturated"]))
          self.assertEqual(value.selected_saturated, vector["saturated"][vector["selected_index"]])

  def test_diagnostic_masks_use_vendor_rotation(self) -> None:
    """An asymmetric recorded patch fails if diagnostic extraction skips the camera rotation."""
    frame = _frames(_VECTORS[0])[0]
    source = replace(_acquisition(), wire_data=frame.source.wire_data)
    diagnostic = CameraWellData(source, frame.mask, correction="none")
    self.assertEqual(diagnostic.measurements[0].pixel_sum, frame.wells[0].pixel_sum)

  def test_zero_exclusion_and_saturation_boundary(self) -> None:
    """Zero pixels are omitted from means; 4000 is allowed and seven higher pixels are flagged."""
    original = _frames(_VECTORS[0])[0]
    points = original.mask.wells[0]
    ring = sorted(
      {(r + dr, c + dc) for r, c in points for dr in (-1, 0, 1) for dc in (-1, 0, 1)} - set(points)
    )
    values = {point: 100 for point in points}
    values.update({point: 10 for point in ring})
    values[points[0]] = values[ring[0]] = 0
    values.update({point: 4001 for point in points[1:8]})
    values[points[8]] = 4000

    def extract() -> FluorescenceFrame:
      """Write synthetic mask-coordinate values into the corresponding original wire positions."""
      wire = list(struct.unpack("<78645H", original.source.wire_data[:157290]))
      for (row, col), value in values.items():
        index = 76799 - row * 320 - col
        wire[321 * (index // 320 + 5) - 1 + index % 320] = value
      source = CameraFrame(original.source.geometry, struct.pack("<78645H", *wire) + bytes(22))
      return replace(original, source=source)

    well = extract().wells[0]
    self.assertEqual(well.pixel_count, len(points) - 1)
    self.assertEqual(well.ring_count, len(ring) - 1)
    self.assertEqual(well.raw_value, well.pixel_sum - 10 * well.pixel_count)
    self.assertEqual(well.saturation_count, 7)
    self.assertTrue(well.saturated)
    values[points[7]] = 4000
    self.assertFalse(extract().wells[0].saturated)

  def test_background_format_and_precision(self) -> None:
    """Reject INI replacement, malformed arrays and nonfinite native CSV coefficients."""
    source = _background(-332.5, 36.497856).source_bytes
    calibration = BackgroundCalibration(source)
    self.assertEqual(calibration.source_bytes, source)
    self.assertNotEqual(
      calibration.slopes[0][0], struct.unpack("<f", struct.pack("<f", 36.4979))[0]
    )
    for invalid in (
      source.replace(b"version = 5", b"version = 4"),
      source + b"extra\n",
      source[:-5],
      source.replace(b"-332.5", b"nan", 1),
      source.replace(b"-332.5,", b"", 1),
      b"[background]\n1=<<eof\n",
    ):
      with self.assertRaises(ValueError):
        BackgroundCalibration(invalid)

  def test_reject_mixed_and_unsupported_frames(self) -> None:
    """A different filter, duplicated exposure, or unsupported pixel depth cannot yield results."""
    frames = _frames(_VECTORS[0])
    bg, uniformity = _background(0, 0), _uniformity(1)
    with self.assertRaisesRegex(ValueError, "distinct"):
      CameraFilterResult((frames[0], frames[0], frames[2]), bg, uniformity, 1, 1, 1)
    with self.assertRaisesRegex(ValueError, "same mask"):
      CameraFilterResult(
        (replace(frames[0], mask=replace(frames[0].mask, filter=2)), frames[1], frames[2]),
        bg,
        uniformity,
        1,
        1,
        1,
      )
    with self.assertRaisesRegex(ValueError, "positive"):
      CameraFilterResult(frames, bg, uniformity, 0, 1, 1)
    with self.assertRaisesRegex(ValueError, "12-bit"):
      replace(
        frames[0], source=CameraFrame(CameraGeometry(321, 245), b"\xff\xff" * 78645 + bytes(22))
      )
    blank = replace(frames[0], source=CameraFrame(CameraGeometry(321, 245), bytes(157312)))
    with self.assertRaisesRegex(ValueError, "nonzero"):
      blank.wells
