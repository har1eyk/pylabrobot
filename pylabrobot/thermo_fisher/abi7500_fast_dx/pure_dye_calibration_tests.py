"""Recorded pure-dye results and independent failure cases; no instrument or vendor runtime."""

import json
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from .acquisition import CameraExposure
from .calibration import _float32, _parse_ini
from .camera import CameraFrame
from .fluorescence_tests import _background
from .optical_calibration_tests import _captured_filter, _native_background, _spectra
from .pure_dye_calibration import (
  PureDyeCalibrationAnalysis,
  PureDyeCalibrationFilter,
  PureDyeCalibrationResult,
  calculate_pure_dye_calibration,
)

_REFERENCE = json.loads(
  (Path(__file__).parent / "test_data/pure_dye_calibration_vectors.json").read_text()
)


class PureDyeAnalysisTests(unittest.TestCase):
  """Check independently recorded outputs and explicitly differing native/Java decisions."""

  def test_all_480_vendor_values_and_distinct_corrections(self) -> None:
    """The Java INI replaces G1; native iterative diagnostics also report B10."""
    bg = _native_background(
      ((0.0,) * 96,) * 5, tuple(tuple(row) for row in _REFERENCE["background_slopes"])
    )
    values = tuple(tuple(row) for row in _REFERENCE["spectra"])
    result = PureDyeCalibrationAnalysis(values, bg)
    self.assertTrue(result.passed)
    self.assertEqual(result.empty_wells, ())
    self.assertEqual(result.corrected_wells, (73,))
    self.assertEqual(result.native_flagged_wells, (73, 22))
    self.assertEqual(
      result.corrected_spectra, tuple(tuple(row) for row in _REFERENCE["normalized"])
    )
    self.assertEqual(result.spectra, values)
    self.assertEqual(result.normalized_spectra[0][21], result.corrected_spectra[0][21])
    self.assertNotEqual(result.normalized_spectra[0][72], result.corrected_spectra[0][72])

  def test_zero_off_peak_channels_and_intensity_scaling(self) -> None:
    """A single-dye spectrum may have zero off-peak channels and varying total intensity."""
    spectra = tuple(tuple((1 + w) * (1000 if f == 3 else 0) for w in range(96)) for f in range(5))
    result = PureDyeCalibrationAnalysis(spectra, _background(0, 0))
    self.assertTrue(result.passed)
    self.assertEqual(result.corrected_wells, ())
    self.assertEqual(result.corrected_spectra[3], (1.0,) * 96)
    self.assertEqual(result.corrected_spectra[0], (0.0,) * 96)

  def test_empty_replacement_and_failures(self) -> None:
    """Empty spectra retain their flags, cannot create NaNs, and cannot bypass quality checks."""
    one = PureDyeCalibrationAnalysis(_spectra({0: (0,) * 5}), _background(0, 0))
    self.assertTrue(one.passed)
    self.assertEqual(one.corrected_wells, (1,))
    self.assertEqual(one.corrected_spectra[0][0], _float32(500000 / 900000))
    json.dumps(one.normalized_spectra, allow_nan=False)
    for changes in ({w: (0,) * 5 for w in range(5)}, {w: (0,) * 5 for w in (0, 1, 12)}):
      result = PureDyeCalibrationAnalysis(_spectra(changes), _background(0, 0))
      self.assertFalse(result.passed)
      self.assertEqual(result.corrected_wells, ())
      with self.assertRaises(ValueError):
        result.corrected_spectra

  def test_invalid_integer_spectra(self) -> None:
    """Malformed matrices, booleans and values outside uint32 are rejected."""
    for bad in ((), ((0,) * 96,) * 4, ((True,) * 96,) * 5, ((-1,) * 96,) * 5):
      with self.subTest(bad=bad), self.assertRaises(ValueError):
        PureDyeCalibrationAnalysis(bad, _background(0, 0))


class PureDyeImageTests(unittest.TestCase):
  """Exercise image averaging and provenance with recorded, compact A1 pixel patches."""

  filters: ClassVar[tuple[PureDyeCalibrationFilter, ...]]

  @classmethod
  def setUpClass(cls) -> None:
    """Reconstruct synthetic full images containing recorded A1 patches for all five filters."""
    cls.filters = tuple(
      PureDyeCalibrationFilter(_captured_filter(v).frames, len(v["patches"]))
      for v in _REFERENCE["patches"]
    )

  def test_recorded_image_extraction(self) -> None:
    """All five fixed collections reproduce the independent full-image A1 integer signals."""
    for item, vector in zip(self.filters, _REFERENCE["patches"]):
      with self.subTest(filter=item.physical_filter):
        bg = _background(vector["background_offset"], vector["background_slope"])
        self.assertEqual(item.spectra(bg), (vector["expected_spectrum"],) * 96)
        self.assertEqual(item.saturated_wells, ())

  def test_incomplete_and_mixed_collections(self) -> None:
    """Missing frames, mismatched exposures and filter masks cannot be treated as complete."""
    item = self.filters[1]
    for frames, count in ((item.frames[:-1], 16), (item.frames, 15), ((), 1)):
      with self.assertRaises(ValueError):
        PureDyeCalibrationFilter(frames, count)
    changed = replace(item.frames[-1], exposure=CameraExposure(0.100))
    with self.assertRaises(ValueError):
      PureDyeCalibrationFilter(item.frames[:-1] + (changed,), 16)
    with self.assertRaises(ValueError):
      PureDyeCalibrationFilter(item.frames[:-1] + self.filters[0].frames, 16)

  def test_clipping_is_preserved_and_blocks_unrepaired_export(self) -> None:
    """Clipped images remain inspectable, but a numerical pass cannot hide unreplaced clipping."""
    item = self.filters[4]
    frame = item.frames[0]
    wire = list(struct.unpack("<78645H", frame.source.wire_data[:157290]))
    for row, column in frame.mask.wells[0][:7]:
      wire[321 * (244 - row) - 1 + (319 - column)] = 4095
    source = CameraFrame(
      frame.source.geometry, struct.pack("<78645H", *wire) + frame.source.wire_data[157290:]
    )
    clipped = PureDyeCalibrationFilter((replace(frame, source=source),) + item.frames[1:], 16)
    self.assertEqual(clipped.saturated_wells, tuple(range(1, 97)))
    result = calculate_pure_dye_calibration(
      self.filters[:4] + (clipped,),
      _background(0, 0),
      dye="ROX",
      instrument_id="unit",
      scanner_id="cam",
      timestamp=1,
      temperature=60,
    )
    self.assertTrue(result.analysis.passed)
    self.assertEqual(result.retained_saturated_wells, tuple(range(1, 97)))
    self.assertIs(result.filters[4].frames[0].source, source)
    with self.assertRaisesRegex(ValueError, "Unreplaced saturated"):
      result.to_ini()

  def test_result_serialization_provenance_and_immutable_inputs(self) -> None:
    """Generated relative spectra reload exactly, while original frames and hashes remain available."""
    result = calculate_pure_dye_calibration(
      self.filters,
      _background(0, 0),
      dye="ROX",
      instrument_id="275030206",
      scanner_id="0000000747",
      timestamp=1,
      temperature=60,
    )
    fields = _parse_ini(result.to_ini())
    values = tuple(
      tuple(_float32(float(v)) for row in fields["ROX"][str(f)] for v in row.split(","))
      for f in range(1, 6)
    )
    self.assertEqual(values, result.analysis.corrected_spectra)
    report = result.report()
    self.assertEqual(report["instrument_id"], "275030206")
    self.assertEqual(report["background_sha256"], result.background.sha256)
    self.assertEqual([len(f["frame_sha256"]) for f in report["filters"]], [1, 16, 16, 16, 16])
    json.dumps(report, allow_nan=False)
    self.assertIs(result.filters, self.filters)
    for filters in (self.filters[:-1], self.filters[:-1] + (self.filters[0],)):
      with self.assertRaises(ValueError):
        PureDyeCalibrationResult(filters, result.background, "ROX", "unit", "cam", 1, 60)
    with self.assertRaises(ValueError):
      replace(result, dye="ROX\n[head]")
    with self.assertRaises(ValueError):
      replace(result, temperature=float("nan"))
