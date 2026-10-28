"""Recorded background-fit regressions, image averaging and guarded offline export."""

import base64
import hashlib
import json
import struct
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from .acquisition import CameraExposure
from .background_calibration import (
  BackgroundCalibrationFilter,
  BackgroundCalibrationGroup,
  BackgroundCalibrationResult,
  _fit_background,
  calculate_background_calibration,
)
from .camera import CameraFrame, CameraGeometry
from .fluorescence import BackgroundCalibration, FluorescenceFrame
from .fluorescence_tests import _mask
from .image_processing import PlatePixelMask

_REFERENCE = json.loads(
  (Path(__file__).parent / "test_data/background_calibration_vectors.json").read_text()
)


def _frame(pixels: list[int], mask: PlatePixelMask, exposure: float) -> FluorescenceFrame:
  """Embed mask-coordinate pixels into original OPB=0 wire coordinates."""
  rotated = list(reversed(pixels))
  wire = [128] * 78645
  for row in range(240):
    start = 321 * (row + 5) - 1
    wire[start : start + 320] = rotated[row * 320 : (row + 1) * 320]
  source = CameraFrame(CameraGeometry(321, 245), struct.pack("<78645H", *wire) + bytes(22))
  return FluorescenceFrame(source, CameraExposure(exposure), mask)


def _captured_group(vector: dict[str, Any], group: dict[str, Any]) -> BackgroundCalibrationGroup:
  """Replay small original patches with a synthetic repeated-well mask; outside pixels are filler."""
  mask = _mask(vector)
  r0, c0, r1, c1 = vector["bounds"]
  frames = []
  for patch_data in group["patches"]:
    pixels = [128] * 76800
    values = iter(
      struct.unpack(
        f"<{(r1 - r0 + 1) * (c1 - c0 + 1)}H", zlib.decompress(base64.b64decode(patch_data))
      )
    )
    for row in range(r0, r1 + 1):
      for column in range(c0, c1 + 1):
        pixels[row * 320 + column] = next(values)
    frames.append(_frame(pixels, mask, group["exposure_ms"] / 1000))
  return BackgroundCalibrationGroup(tuple(frames), group["expected_frame_count"])


class BackgroundFitTests(unittest.TestCase):
  """Compare with independent full-precision vendor coefficients, not rounded CSV exports."""

  def test_all_960_vendor_coefficients(self) -> None:
    """Recorded raw counts reproduce all slopes exactly and offsets within 1e-8 FSU."""
    for physical, groups in enumerate(_REFERENCE["raw_physical_order"], 1):
      for well in range(96):
        offset, slope = _fit_background(tuple(group[well] for group in groups))
        self.assertAlmostEqual(offset, _REFERENCE["offsets"][5 - physical][well], delta=1e-8)
        self.assertEqual(slope, _REFERENCE["slopes"][5 - physical][well])

  def test_zero_background(self) -> None:
    """Zero inputs remain zero without a fabricated positive background floor."""
    self.assertEqual(_fit_background((0, 0, 0)), (0.0, 0.0))


class BackgroundCameraTests(unittest.TestCase):
  """Cover complete frame extraction, mixed-input rejection and precision-preserving output."""

  captured: ClassVar[tuple[BackgroundCalibrationFilter, ...]]
  result: ClassVar[BackgroundCalibrationResult]

  @classmethod
  def setUpClass(cls) -> None:
    """Decode two bounded captured regions, then create explicitly synthetic extra channels."""
    cls.captured = tuple(
      BackgroundCalibrationFilter(tuple(_captured_group(vector, g) for g in vector["groups"]))
      for vector in _REFERENCE["patches"]
    )
    filters = [cls.captured[0]]
    for physical in range(2, 5):
      groups = tuple(
        replace(
          group,
          frames=tuple(
            replace(frame, mask=replace(frame.mask, filter=physical)) for frame in group.frames
          ),
        )
        for group in cls.captured[0].groups
      )
      filters.append(BackgroundCalibrationFilter(groups))
    filters.append(cls.captured[1])
    cls.result = calculate_background_calibration(
      tuple(reversed(filters)), instrument_id="275030206", scanner_id="0000000747", timestamp=1
    )

  def test_captured_pixels_to_vendor_fit(self) -> None:
    """All 81 captured patches match independently retained raw averages and vendor fits."""
    for item, vector in zip(self.captured, _REFERENCE["patches"]):
      for group, expected in zip(item.ordered_groups, vector["groups"]):
        self.assertEqual(group.raw_values, (expected["raw_value"],) * 96)
        self.assertEqual(group.saturated_wells, ())
      offset, slope = item.coefficients[0]
      saved, well = item.saved_filter - 1, vector["source_well"] - 1
      self.assertAlmostEqual(offset, _REFERENCE["offsets"][saved][well], delta=1e-8)
      self.assertEqual(slope, _REFERENCE["slopes"][saved][well])

  def test_average_before_clipping_and_truncation(self) -> None:
    """Negative fractions must participate in the mean rather than being clipped per frame."""
    mask = _mask({"mask_pixels": [[10, 10], [10, 11]], "physical_filter": 1})
    pixels = [100] * 76800
    pixels[9 * 320 + 9] = 103  # Ten ring pixels: mean 100.3.
    negative = _frame(pixels, mask, 2)
    pixels[10 * 320 + 10] = 103
    positive = _frame(pixels, mask, 2)
    group = BackgroundCalibrationGroup((negative,) * 8 + (positive,) * 8, 16)
    self.assertEqual(group.raw_values, (0,) * 96)  # Mean of -0.6 and 2.4 is 0.9.
    self.assertEqual(positive.wells[0].raw_value, 2)

  def test_export_precision_provenance_and_no_usb(self) -> None:
    """Export reloads all float32 bits, retains source hashes and performs no device setup."""
    with patch("pylabrobot.io.usb.USB.setup", side_effect=AssertionError("No hardware")):
      result = self.result
      self.assertTrue(result.passed)
      output = result.to_background()
      reloaded = BackgroundCalibration(output.source_bytes)
      self.assertEqual(reloaded.offsets, result.offsets)
      self.assertEqual(reloaded.slopes, result.slopes)
      self.assertEqual(output.source_bytes, result.to_background().source_bytes)
      report = result.report()
    self.assertEqual(report["background_sha256"], output.sha256)
    self.assertEqual([item["physical_filter"] for item in report["filters"]], [1, 2, 3, 4, 5])
    first = self.captured[0].groups[0].frames[0]
    self.assertEqual(report["filters"][0]["mask_sha256"], first.mask.sha256)
    self.assertEqual(
      report["filters"][0]["groups"][0]["frame_sha256"],
      [hashlib.sha256(first.source.wire_data).hexdigest()],
    )
    self.assertEqual(result.offsets[4], tuple(v[0] for v in self.captured[0].coefficients))
    json.dumps(report, allow_nan=False)

  def test_incomplete_mixed_and_unsupported_groups(self) -> None:
    """Partial frames, undocumented counts, search exposures and mixed settings are rejected."""
    group = self.captured[0].groups[1]
    for frames, count, message in (
      (group.frames[:-1], 16, "Incomplete"),
      ((), 1, "Incomplete"),
      (group.frames, True, "expected count"),
      (group.frames[:2], 2, "expected count"),
      ((replace(group.frames[0], exposure=CameraExposure(0.256)),), 1, "exposures"),
      ((self.captured[0].groups[0].frames[0],) + group.frames[1:], 16, "same mask"),
      ((self.captured[1].groups[1].frames[0],) + group.frames[1:], 16, "same mask"),
    ):
      with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
        BackgroundCalibrationGroup(frames, count)
    with self.assertRaises(TypeError):
      BackgroundCalibrationGroup((None,), 1)  # type: ignore[arg-type]

  def test_missing_duplicate_exposures_and_masks(self) -> None:
    """Each filter requires all three exposures and the identical ROI source."""
    a, b = self.captured
    for groups, message in (
      (a.groups[:2], "three"),
      ((a.groups[0],) * 3, "exactly one"),
      ((b.groups[0],) + a.groups[1:], "same mask"),
    ):
      with self.assertRaisesRegex(ValueError, message):
        BackgroundCalibrationFilter(groups)
    reordered = BackgroundCalibrationFilter(tuple(reversed(a.groups)))
    self.assertEqual(reordered.coefficients, a.coefficients)

  def test_invalid_result_provenance(self) -> None:
    """Missing/duplicate filters, empty IDs and malformed timestamps cannot create a result."""
    for fields in (
      {"filters": self.result.filters[:-1]},
      {"filters": (self.result.filters[0],) * 5},
      {"instrument_id": ""},
      {"scanner_id": "camera\nother"},
      {"timestamp": True},
      {"timestamp": -1},
    ):
      with self.assertRaises(ValueError):
        replace(self.result, **fields)

  def test_saturation_and_high_background_block_export(self) -> None:
    """Failures retain diagnostics but never produce usable calibration files or hide bad wells."""
    original = self.captured[0]
    for intensity, saturated in ((4095, True), (3000, False)):
      # A roughly 300-pixel region above a low ring makes a high background even without clipping.
      groups = []
      for group in original.ordered_groups:
        pixels = [128] * 76800
        for row, column in group.frames[0].mask.wells[0]:
          pixels[row * 320 + column] = intensity
        frame = _frame(pixels, group.frames[0].mask, group.frames[0].exposure.duration)
        groups.append(BackgroundCalibrationGroup((frame,), 1))
      bad = BackgroundCalibrationFilter(tuple(groups))
      self.assertEqual(bool(bad.groups[0].saturated_wells), saturated)
      self.assertEqual(bad.above_threshold, tuple(range(1, 97)))
      result = replace(self.result, filters=self.result.filters[:-1] + (bad,))
      self.assertFalse(result.passed)
      with self.assertRaisesRegex(ValueError, "Background calibration failed"):
        result.to_background()
      report = result.report()
      self.assertIsNone(report["background_sha256"])
      self.assertIsNotNone(report["offsets"])
      json.dumps(report, allow_nan=False)

  def test_reference_threshold_channel_mapping_and_boundary(self) -> None:
    """Physical 1 is saved E; equality passes, and only values strictly above fail."""
    for item in self.result.filters:
      limit = 90000.0 if item.physical_filter == 1 else 72000.0
      self.assertEqual(item.threshold, limit)
      with patch.object(
        BackgroundCalibrationFilter, "signals", new_callable=unittest.mock.PropertyMock
      ) as signals:
        signals.return_value = (limit, limit + 1) + (0.0,) * 94
        self.assertEqual(item.above_threshold, (2,))
