"""Recorded raw-image calibration and synthetic quality failures, without instrument I/O."""

import base64
import json
import struct
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, Mapping

from .acquisition import CameraExposure
from .calibration import UniformityCalibration, _coefficient
from .camera import CameraFrame, CameraGeometry
from .fluorescence import BackgroundCalibration, FluorescenceFrame
from .fluorescence_tests import _background, _mask
from .optical_calibration import (
  OpticalCalibrationAnalysis,
  OpticalCalibrationFilter,
  calculate_optical_calibration,
)

_REFERENCE = json.loads(
  (Path(__file__).parent / "test_data/optical_calibration_vectors.json").read_text()
)


def _native_background(
  offsets: tuple[tuple[float, ...], ...], slopes: tuple[tuple[float, ...], ...]
) -> BackgroundCalibration:
  """Serialize numeric arrays with the existing native CSV header structure."""
  lines = _background(0, 0).source_bytes.decode().splitlines()
  for index in range(5):
    lines[index + 2] = ",".join(map(str, offsets[index])) + ","
    lines[index + 8] = ",".join(map(str, slopes[index])) + ","
  return BackgroundCalibration(("\n".join(lines) + "\n").encode())


def _captured_filter(vector: dict[str, Any]) -> OpticalCalibrationFilter:
  """Place recorded A1 patches in wire coordinates with a synthetic repeated-well mask."""
  mask = _mask(vector)
  r0, c0, r1, c1 = vector["bounds"]
  frames = []
  for patch, exposure in zip(vector["patches"], vector["exposures"]):
    pixels = [128] * 76800
    values = iter(
      struct.unpack(f"<{(r1 - r0 + 1) * (c1 - c0 + 1)}H", zlib.decompress(base64.b64decode(patch)))
    )
    for row in range(r0, r1 + 1):
      for column in range(c0, c1 + 1):
        pixels[row * 320 + column] = next(values)
    pixels.reverse()
    wire = [128] * 78645
    for row in range(240):
      start = 321 * (row + 5) - 1
      wire[start : start + 320] = pixels[row * 320 : (row + 1) * 320]
    source = CameraFrame(CameraGeometry(321, 245), struct.pack("<78645H", *wire) + bytes(22))
    frames.append(FluorescenceFrame(source, CameraExposure(exposure), mask))
  return OpticalCalibrationFilter(tuple(frames), len(frames))


def _spectra(changes: Mapping[int, tuple[int, ...]]) -> tuple[tuple[int, ...], ...]:
  """Make a uniform 96-well spectrum with explicitly indexed exceptions (zero-based)."""
  normal = (500000, 600000, 700000, 800000, 900000)
  return tuple(tuple(changes.get(well, normal)[f] for well in range(96)) for f in range(5))


class OpticalAnalysisTests(unittest.TestCase):
  """Check vendor-derived factors and general failure behavior, with no reference factors as input."""

  def test_all_recorded_coefficients_and_automatic_outlier(self) -> None:
    """Recover every vendor coefficient and H5 automatically from 480 saved raw signals."""
    bg = _native_background(
      ((0.0,) * 96,) * 5, tuple(tuple(row) for row in _REFERENCE["background_slopes"])
    )
    spectra = tuple(tuple(row) for row in _REFERENCE["spectra"])
    result = OpticalCalibrationAnalysis(spectra, bg)
    self.assertTrue(result.passed)
    self.assertEqual(result.empty_wells, ())
    self.assertEqual(result.outlier_wells, (89,))
    self.assertEqual(result.corrected_wells, (89,))
    self.assertEqual(
      result.coefficient_text, tuple(tuple(row) for row in _REFERENCE["coefficient_text"])
    )
    self.assertEqual(result.spectra, spectra)
    self.assertNotEqual(result.corrected_spectra[0][88], spectra[0][88])
    self.assertEqual(result.corrected_spectra[0][87], spectra[0][87])

  def test_uniform_plate_and_intensity_gradient(self) -> None:
    """Intensity-only variation needs optical normalization, not spectral-outlier replacement."""
    changes = {well: tuple((f + 5) * (1000 + well * 100) for f in range(5)) for well in range(96)}
    result = OpticalCalibrationAnalysis(_spectra(changes), _background(0, 0))
    self.assertTrue(result.passed)
    self.assertEqual(result.corrected_wells, ())
    self.assertGreater(result.coefficients[0][0], result.coefficients[0][-1])
    flat = OpticalCalibrationAnalysis(_spectra({}), _background(0, 0))
    self.assertEqual(flat.coefficients, ((1.0,) * 96,) * 5)

  def test_empty_boundary_and_neighbor_replacement(self) -> None:
    """Twice-background is a strict boundary; a single empty well uses adjacent good wells."""
    below = OpticalCalibrationAnalysis(_spectra({0: (1999,) * 5}), _background(0, 1))
    at = OpticalCalibrationAnalysis(_spectra({0: (2000,) * 5}), _background(0, 1))
    self.assertEqual(below.empty_wells, (1,))
    self.assertEqual(at.empty_wells, ())
    empty = OpticalCalibrationAnalysis(_spectra({0: (0,) * 5}), _background(0, 0))
    self.assertEqual(empty.corrected_wells, (1,))
    self.assertEqual(empty.corrected_spectra[0][0], 500000)
    self.assertEqual(empty.coefficients, ((1.0,) * 96,) * 5)

  def test_failure_does_not_produce_coefficients(self) -> None:
    """Too many defects, a corner cluster and a missing channel cannot pass silently."""
    for changes, message in (
      ({i: (0,) * 5 for i in range(5)}, "Five or more"),
      ({i: (0,) * 5 for i in (0, 1, 12)}, "fewer than two"),
      ({i: (0, 600000, 700000, 800000, 900000) for i in range(96)}, "nonpositive"),
      ({i: (0,) * 5 for i in range(96)}, "Five or more"),
    ):
      with self.subTest(message=message):
        result = OpticalCalibrationAnalysis(_spectra(changes), _background(0, 0))
        self.assertFalse(result.passed)
        self.assertEqual(result.corrected_wells, ())
        with self.assertRaisesRegex(ValueError, message):
          result.coefficients

  def test_empty_filter_ties_use_binary32_and_physical_order(self) -> None:
    """Two distinct uint32 signals can tie after the native conversion to float."""
    slopes = tuple(
      tuple(10000.0 if f == 4 and w == 0 else 0.0 for w in range(96)) for f in range(5)
    )
    background = _native_background(((0.0,) * 96,) * 5, slopes)
    result = OpticalCalibrationAnalysis(
      _spectra({0: (16777217, 600000, 700000, 800000, 16777216)}), background
    )
    self.assertEqual(result.empty_wells, (1,))

  def test_spectral_outliers_iterate_and_are_bounded(self) -> None:
    """Strong and moderate spectral defects are removed on successive passes."""
    result = OpticalCalibrationAnalysis(
      _spectra({10: (9000000, 1, 1, 1, 1), 40: (500000, 600000, 1000000, 800000, 900000)}),
      _background(0, 0),
    )
    self.assertTrue(result.passed)
    self.assertEqual(set(result.outlier_wells), {11, 41})
    self.assertEqual(result.coefficients, ((1.0,) * 96,) * 5)

  def test_invalid_spectra(self) -> None:
    """Reject shape errors, floats, bools and overflow before statistical processing."""
    for invalid in (
      (),
      ((1,) * 95,) * 5,
      ((-1,) * 96,) * 5,
      ((2**32,) * 96,) * 5,
      ((True,) * 96,) * 5,
      ((1.0,) * 96,) * 5,
    ):
      with self.assertRaises(ValueError):
        OpticalCalibrationAnalysis(invalid, _background(0, 0))  # type: ignore[arg-type]


class OpticalCameraTests(unittest.TestCase):
  """Exercise original frame decoding, averaging, filter order and output provenance."""

  filters: ClassVar[tuple[OpticalCalibrationFilter, ...]]
  background: ClassVar[BackgroundCalibration]

  @classmethod
  def setUpClass(cls) -> None:
    """Decode the bounded captured patches once; no USB driver is instantiated."""
    cls.filters = tuple(_captured_filter(vector) for vector in _REFERENCE["patches"])
    ordered = list(reversed(_REFERENCE["patches"]))
    cls.background = _native_background(
      tuple((v["background_offset"],) * 96 for v in ordered),
      tuple((v["background_slope"],) * 96 for v in ordered),
    )

  def test_recorded_images_match_spectra(self) -> None:
    """All 65 A1 patches match the native integer result, including pre-truncation averaging."""
    for collection, vector in zip(self.filters, _REFERENCE["patches"]):
      self.assertEqual(collection.spectra(self.background), (vector["expected_spectrum"],) * 96)
      self.assertEqual(collection.saved_filter, 6 - vector["physical_filter"])

  def test_result_roundtrip_and_provenance(self) -> None:
    """The generated calibration works with the existing reader and records inputs exactly."""
    result = calculate_optical_calibration(
      tuple(reversed(self.filters)),
      self.background,
      instrument_id="275030206",
      scanner_id="0000000747",
      timestamp=1792423403979,
    )
    output = result.to_uniformity()
    # Sequential binary32 accumulation gives a small error even for repeated equal signals.
    self.assertEqual(
      output.coefficients,
      tuple((_coefficient(v),) * 96 for v in ("1", "1", "1", "0.999999", "1")),
    )
    self.assertEqual(output.instrument_id, "275030206")
    self.assertEqual(output.scanner_id, "0000000747")
    self.assertEqual(UniformityCalibration(output.source_bytes), output)
    self.assertEqual(result.to_uniformity().source_bytes, output.source_bytes)
    report = result.report()
    self.assertEqual(report["background_sha256"], self.background.sha256)
    self.assertEqual([f["expected_frame_count"] for f in report["filters"]], [1, 16, 16, 16, 16])
    json.dumps(report, allow_nan=False)
    self.assertEqual(_coefficient(result.analysis.coefficient_text[0][0]), 1.0)

  def test_incomplete_mixed_and_saturated_frames(self) -> None:
    """Search/partial/mixed frames and clipping cannot enter a final collection."""
    group = self.filters[1]
    with self.assertRaisesRegex(ValueError, "Incomplete"):
      replace(group, frames=group.frames[:-1])
    with self.assertRaisesRegex(ValueError, "same"):
      replace(group, frames=(self.filters[0].frames[0],) + group.frames[1:])
    with self.assertRaisesRegex(ValueError, "same"):
      replace(
        group, frames=(replace(group.frames[0], exposure=CameraExposure(0.5)),) + group.frames[1:]
      )
    with self.assertRaisesRegex(ValueError, "1 or 16"):
      replace(group, expected_frame_count=2)
    frame = group.frames[0]
    wire = struct.pack("<78645H", *([4095] * 78645)) + bytes(22)
    saturated = replace(frame, source=CameraFrame(frame.source.geometry, wire))
    with self.assertRaisesRegex(ValueError, "Saturated"):
      OpticalCalibrationFilter((saturated,), 1)

  def test_missing_filters_and_invalid_identity(self) -> None:
    """Missing or duplicate filters and INI field injection fail before producing a file."""
    for filters, instrument, timestamp in (
      (self.filters[:-1], "test", 0),
      ((self.filters[0],) * 5, "test", 0),
      (self.filters, "test\n[result]\nresult=true", 0),
      (self.filters, "test", True),
    ):
      with self.assertRaises(ValueError):
        calculate_optical_calibration(
          filters,
          self.background,
          instrument_id=instrument,
          scanner_id="camera",
          timestamp=timestamp,
        )
