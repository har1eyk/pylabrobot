"""Saved vendor signals, quality boundaries and input provenance, without instrument access."""

import hashlib
import json
import math
import struct
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from ._spectral_math import spectral_inverse, spectral_signals
from .calibration import _float32, apply_uniformity
from .fluorescence import CameraFilterResult
from .fluorescence_tests import _VECTORS, _background, _frames, _uniformity
from .results import SavedFilterData
from .spectral import (
  DyeCalibration,
  FilterSignal,
  WellFilterSignals,
  read_eds_dye_calibration,
  unmix_collection,
  unmix_well,
)

_FIXTURE = json.loads((Path(__file__).parent / "test_data/spectral_vectors.json").read_text())


def _calibration(
  dyes: tuple[str, ...] = ("GREEN", "ROX"),
  spectra: tuple[tuple[float, ...], ...] = (
    (1.0, 0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0, 0.0),
  ),
) -> bytes:
  """Repeat supplied spectra in all wells of an explicitly synthetic INI."""
  text = (
    "[head]\nversion=1.0.0\n[instrument]\nid=test\ntype=sds7500fast\n"
    "[scanner]\nid=test\nfilters=1,2,3,4,5\n[puredyes]\ndyes=" + ",".join(dyes) + "\n"
  )
  for dye, spectrum in zip(dyes, spectra):
    text += f"[{dye}]\ncalibration-time=1\nexpiration-time=0\ntemperature=60.0\nresult=true\n"
    for index, value in enumerate(spectrum, 1):
      text += f"{index}=<<eof\n" + (",".join([str(value)] * 12) + "\n") * 8 + "eof\n"
  return text.encode()


def _source(
  values: tuple[float, ...] = (100.0, 0.0, 0.0, 20.0, 0.0),
) -> WellFilterSignals:
  """Return a known orthogonal reporter/reference example with explicit saturation status."""
  return WellFilterSignals(
    1,
    2,
    1,
    1,
    tuple(FilterSignal(index, value, False) for index, value in enumerate(values, 1)),
    "synthetic:orthogonal",
  )


def _dyes(source: bytes | None = None) -> tuple[DyeCalibration, ...]:
  """Load the two explicitly named synthetic dyes."""
  raw = _calibration() if source is None else source
  return DyeCalibration(raw, "GREEN"), DyeCalibration(raw, "ROX")


class SpectralReplayTests(unittest.TestCase):
  """Compare independent saved numeric values with the complete public single-well path."""

  def test_saved_dye_components_residuals_and_rn(self) -> None:
    """Fixtures cover one, two and three dyes, weak/negative references and late cycles."""
    for case in _FIXTURE["cases"]:
      with self.subTest(dataset=case["dataset"], well=case["well"], cycle=case["cycle"]):
        raw = _calibration(tuple(case["dyes"]), tuple(tuple(row) for row in case["spectra"]))
        dyes = tuple(DyeCalibration(raw, name) for name in case["dyes"])
        values = tuple(
          _float32(_float32(raw_value) * factor)
          for raw_value, factor in zip(case["raw"], case["uniformity"])
        )
        self.assertEqual(values, tuple(case["corrected"]))
        source = WellFilterSignals(
          case["well"],
          case["stage"],
          case["cycle"],
          case["collection_point"],
          tuple(FilterSignal(f + 1, value, None) for f, value in enumerate(values)),
          _FIXTURE["sources"][case["dataset"]]["sha256"],
        )
        result = unmix_well(
          source,
          dyes,
          reporter=case["reporter"],
          passive_reference=case["passive_reference"],
        )
        self.assertEqual(
          tuple(value for _, value in result.components), tuple(case["expected_signals"])
        )
        self.assertEqual(result.residual_standard_error, case["expected_residual"])
        if case["passive_reference"] is not None:
          self.assertEqual(result.unrestricted_rn, case["expected_rn"])
        else:
          self.assertIsNone(result.unrestricted_rn)
          self.assertEqual(dict(result.components)[case["reporter"]], case["expected_rn"])
        self.assertIsNone(result.rn)
        self.assertIn("saturation_unknown", result.flags)
        self.assertIs(result.source, source)
        self.assertEqual(result.calibrations, dyes)
        json.dumps(result.report(), allow_nan=False)

  def test_dye_normalization_uses_all_filters_before_subset(self) -> None:
    """The dye peak is global, even when its strongest filter is not selected."""
    raw = _calibration(("GREEN", "ROX"), ((2, 0.5, 0, 0, 0), (0, 0, 0, 4, 1)))
    dyes = _dyes(raw)
    result = unmix_well(_source((0, 25, 0, 0, 5)), dyes, reporter="GREEN", enabled_filters=(2, 5))
    self.assertEqual(dict(result.components), {"GREEN": 100, "ROX": 20})
    self.assertEqual(result.rn, 5)
    self.assertIsNone(result.residual_standard_error)

  def test_signed_components_are_not_clipped_to_nonnegative(self) -> None:
    """A negative reporter survives for diagnosis and prevents a guarded Rn."""
    result = unmix_well(_source((-100, 0, 0, 20, 0)), _dyes(), reporter="GREEN")
    self.assertEqual(dict(result.components)["GREEN"], -100)
    self.assertEqual(result.unrestricted_rn, -5)
    self.assertIn("negative_component", result.flags)
    self.assertIsNone(result.rn)

  def test_reference_boundaries_and_finite_reports(self) -> None:
    """A zero denominator never produces infinity; tiny positive references are flagged."""
    for reference, usable, flag in (
      (20, True, None),
      (10, True, None),
      (9.99, False, "low_reference"),
      (0, False, "nonpositive_reference"),
      (-1, False, "nonpositive_reference"),
      (1e-40, False, "nonfinite_ratio"),
    ):
      result = unmix_well(_source((100, 0, 0, reference, 0)), _dyes(), reporter="GREEN")
      if flag:
        self.assertIn(flag, result.flags)
      self.assertEqual(result.rn is not None, usable)
      if reference == 0:
        self.assertIsNone(result.unrestricted_rn)
      json.dumps(result.report(), allow_nan=False)

  def test_missing_data_is_not_a_zero_or_implicit_filter_subset(self) -> None:
    """Missing requested channels block unmixing even when the other channels suffice."""
    original = _source()
    for signals in (
      original.signals[:-1],
      original.signals[:-1] + (FilterSignal(5, None, False),),
      (),
    ):
      result = unmix_well(replace(original, signals=signals), _dyes(), reporter="GREEN")
      self.assertEqual(result.components, ())
      self.assertIn("missing_filter", result.flags)
      self.assertIsNone(result.unrestricted_rn)
    result = unmix_well(original, _dyes(), reporter="GREEN", enabled_filters=(1, 4))
    self.assertEqual(result.rn, 5)

  def test_saturation_unknown_and_review_flags_preserve_diagnostic_values(self) -> None:
    """Flagged G1 or calibration repairs cannot silently become accepted measurements."""
    source = _source()
    for saturation, flag in ((True, "saturated"), (None, "saturation_unknown")):
      changed = replace(
        source, signals=(replace(source.signals[0], saturated=saturation),) + source.signals[1:]
      )
      result = unmix_well(changed, _dyes(), reporter="GREEN")
      self.assertIn(flag, result.flags)
      self.assertEqual(result.unrestricted_rn, 5)
      self.assertIsNone(result.rn)
    reviewed = replace(source, well=73, review_reasons=("Unresolved G1 repeatability",))
    result = unmix_well(reviewed, _dyes(), reporter="GREEN")
    self.assertIn("review_required", result.flags)
    self.assertEqual(result.report()["review_reasons"], reviewed.review_reasons)
    self.assertIsNone(result.rn)
    dyes = _dyes()
    result = unmix_well(source, (replace(dyes[0], review_wells=(1,)), dyes[1]), reporter="GREEN")
    self.assertIn("review_required", result.flags)

  def test_singular_near_singular_and_empty_spectra_fail_closed(self) -> None:
    """No hidden regularization creates finite-looking dye measurements."""
    for spectra in (
      ((1, 0, 0, 0, 0), (1, 0, 0, 0, 0)),
      ((1, 0, 0, 0, 0), (1, 0.000001, 0, 0, 0)),
      ((1, 0, 0, 0, 0), (0, 0, 0, 0, 0)),
    ):
      result = unmix_well(_source(), _dyes(_calibration(spectra=spectra)), reporter="GREEN")
      self.assertIn("invalid_spectral_calibration", result.flags)
      self.assertEqual(result.components, ())
      self.assertIsNone(result.rn)

  def test_square_and_tall_linear_systems(self) -> None:
    """Independent basis cases cover up to five dyes and preserve signed solutions."""
    for size in range(1, 6):
      matrix = tuple(tuple(float(r == c) for c in range(size)) for r in range(5))
      inverse, condition = spectral_inverse(matrix, 100)
      signals = spectral_signals(inverse, (3, -2, 4, 1, 8))
      self.assertEqual(signals, (3, -2, 4, 1, 8)[:size])
      self.assertEqual(condition, 1)


class DyeCalibrationTests(unittest.TestCase):
  """Validate calibration provenance and explicit diagnostic-only failure handling."""

  def test_provenance_line_endings_and_exact_dye_names(self) -> None:
    """Identical coefficients do not imply identical source bytes or interchangeable dyes."""
    source = _calibration()
    a, b = DyeCalibration(source, "GREEN"), DyeCalibration(source.replace(b"\n", b"\r\n"), "GREEN")
    self.assertEqual(a.coefficients, b.coefficients)
    self.assertNotEqual(a.sha256, b.sha256)
    self.assertEqual(a.sha256, hashlib.sha256(source).hexdigest())
    self.assertEqual(a.calibration_time, 1)
    self.assertEqual(a.expiration_time, 0)
    self.assertEqual(a.temperature, 60)
    self.assertEqual((a.instrument_id, a.scanner_id), ("test", "test"))
    with self.assertRaises(ValueError):
      DyeCalibration(source, "EvaGreen")
    with self.assertRaises(ValueError):
      unmix_well(_source(), _dyes(), reporter="FAM")

  def test_failed_calibration_requires_explicit_diagnostic_option(self) -> None:
    """Historical failed metadata is never silently promoted to an accepted calibration."""
    failed = _calibration().replace(b"result=true", b"result=false", 1)
    with self.assertRaisesRegex(ValueError, "Failed dye"):
      DyeCalibration(failed, "GREEN")
    green = DyeCalibration(failed, "GREEN", allow_failed=True)
    result = unmix_well(_source(), (green, DyeCalibration(failed, "ROX")), reporter="GREEN")
    self.assertEqual(result.unrestricted_rn, 5)
    self.assertIsNone(result.rn)
    self.assertIn("calibration_failed", result.flags)
    self.assertFalse(result.report()["calibrations"][0]["saved_passed"])

  def test_conflicting_instrument_and_scanner_provenance(self) -> None:
    """Known IDs and model types cannot be mixed just because matrices have the same shape."""
    raw = _calibration()
    for changed in (
      raw.replace(b"[instrument]\nid=test", b"[instrument]\nid=other"),
      raw.replace(b"[scanner]\nid=test", b"[scanner]\nid=other"),
      raw.replace(b"sds7500fast", b"sds7500"),
    ):
      with self.assertRaisesRegex(ValueError, "conflict"):
        unmix_well(
          _source(),
          (DyeCalibration(raw, "GREEN"), DyeCalibration(changed, "ROX")),
          reporter="GREEN",
        )
    unspecified = raw.replace(b"id=test", b"id=null")
    result = unmix_well(_source(), _dyes(unspecified), reporter="GREEN")
    self.assertIn("calibration_identity_unspecified", result.flags)

  def test_invalid_calibration_layouts_and_numbers(self) -> None:
    """Reject truncated, reordered, nonfinite, negative and duplicated calibration fields."""
    raw = _calibration()
    for bad in (
      raw.replace(b"filters=1,2,3,4,5", b"filters=5,4,3,2,1"),
      raw.replace(b"sds7500fast", b"sds7300"),
      raw.replace(b"1.0,", b"nan,", 1),
      raw.replace(b"1.0,", b"-0.1,", 1),
      raw.replace(b"1.0,", b"1e99,", 1),
      raw.replace(b"1.0,", b"", 1),
      raw.replace(b"calibration-time=1", b"calibration-time=-1"),
      raw.replace(b"result=true", b"result=maybe"),
      raw.replace(b"dyes=GREEN,ROX", b"dyes=GREEN,GREEN,ROX"),
      raw + b"[head]\nversion=1.0.0\n",
      raw.replace(b"[GREEN]", b"[GREEN]\n1=5"),
    ):
      with self.subTest(source=bad[:100]), self.assertRaises(ValueError):
        DyeCalibration(bad, "GREEN")
    with self.assertRaises(ValueError):
      DyeCalibration(raw[:-4], "GREEN")
    self.assertEqual(
      DyeCalibration(raw.replace(b"0.0,", b"0e0,"), "GREEN").spectrum(1), (1, 0, 0, 0, 0)
    )

  def test_eds_reader_rejects_ambiguous_members_without_extracting(self) -> None:
    """Two spellings of the calibration path cannot select different files silently."""
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "experiment.eds"
      with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("APLDBIO\\SDS\\CALIBRATIONS/puredye.ini", _calibration())
        archive.writestr("../outside.txt", b"never extracted")
      dye = read_eds_dye_calibration(path, "GREEN")
      self.assertEqual(dye.source_bytes, _calibration())
      self.assertEqual(list(Path(directory).iterdir()), [path])
      with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("apldbio/sds/calibrations/puredye.ini", _calibration())
      with self.assertRaises(ValueError):
        read_eds_dye_calibration(path, "GREEN")

  def test_invalid_input_objects_and_policies(self) -> None:
    """Indices, provenance, duplicate channels and numeric policies fail before fitting."""
    for index in (0, 6, True):
      with self.assertRaises(ValueError):
        FilterSignal(index, 1, False)
    for value in (math.nan, math.inf, 1e39, True):
      with self.assertRaises(ValueError):
        FilterSignal(1, value, False)
    source = _source()
    for changes in (
      {"well": 0},
      {"well": 97},
      {"cycle": -1},
      {"collection_point": True},
      {"stage": 0},
      {"source_id": ""},
      {"signals": source.signals + (source.signals[0],)},
      {"review_reasons": ("",)},
    ):
      with self.assertRaises(ValueError):
        replace(source, **changes)
    for options in (
      {"enabled_filters": (1, 1)},
      {"enabled_filters": (4, 1)},
      {"enabled_filters": (1,)},
      {"minimum_reference": -1},
      {"minimum_reference": math.nan},
      {"max_condition": 0.1},
      {"passive_reference": "GREEN"},
    ):
      with self.assertRaises(ValueError):
        unmix_well(source, _dyes(), reporter="GREEN", **options)


class SpectralCollectionTests(unittest.TestCase):
  """Exercise the saved-array and camera adapters and preservation of collection identity."""

  def saved_results(self) -> tuple:
    """Build five saved arrays with independently specified orthogonal signals."""
    results = []
    for f, value in enumerate((100, 0, 0, 20, 0), 1):
      source = SavedFilterData.decode(
        f"apldbio/sds/images/stage2-cycle001-point1-{f}.tiff", struct.pack(">96q", *([value] * 96))
      )
      results.append(apply_uniformity(source, _uniformity(1)))
    return tuple(results)

  def test_saved_collection_retains_indices_saturation_unknown_and_provenance(self) -> None:
    """All wells retain the source point and cannot claim saturation-free saved data."""
    results = self.saved_results()
    rows = unmix_collection(results, _dyes(), reporter="GREEN", review_wells=(73,))
    self.assertEqual(len(rows), 96)
    for well, row in enumerate(rows, 1):
      self.assertEqual(
        (row.source.well, row.source.stage, row.source.cycle, row.source.collection_point),
        (well, 2, 1, 1),
      )
      self.assertEqual(row.unrestricted_rn, 5)
      self.assertIsNone(row.rn)
      self.assertIn("saturation_unknown", row.flags)
    self.assertIn("review_required", rows[72].flags)
    self.assertEqual(
      rows, unmix_collection(results[::-1], _dyes(), reporter="GREEN", review_wells=(73,))
    )
    changed = (apply_uniformity(results[0].source, _uniformity(2)),) + results[1:]
    with self.assertRaisesRegex(ValueError, "calibrations differ"):
      unmix_collection(changed, _dyes(), reporter="GREEN")
    with self.assertRaisesRegex(ValueError, "Duplicate"):
      unmix_collection(results + (results[0],), _dyes(), reporter="GREEN")
    other = SavedFilterData.decode(
      "apldbio/sds/images/stage2-cycle002-point1-1.tiff", results[0].source.source_bytes
    )
    with self.assertRaisesRegex(ValueError, "point"):
      unmix_collection(
        (apply_uniformity(other, _uniformity(1)),) + results[1:], _dyes(), reporter="GREEN"
      )

  def test_camera_adapter_uses_saved_filter_direction_and_original_exposures(self) -> None:
    """Reconstructed frames enter at the existing pipeline output, with known saturation."""
    frames = _frames(_VECTORS[0])
    result = CameraFilterResult(frames, _background(0, 0), _uniformity(1), 2, 1, 1)
    spectrum = tuple(float(f == result.filter) for f in range(1, 6))
    dye = DyeCalibration(_calibration(("GREEN",), (spectrum,)), "GREEN")
    measurements = unmix_collection(
      (result,),
      (dye,),
      reporter="GREEN",
      passive_reference=None,
      enabled_filters=(result.filter,),
    )
    self.assertEqual(
      dict(measurements[0].components)["GREEN"], result.measurements[0].corrected_value
    )
    self.assertNotIn("saturation_unknown", measurements[0].flags)
    self.assertEqual(
      measurements[0].source.signals[0].saturated, result.measurements[0].selected_saturated
    )
    changed_frame = replace(frames[0], exposure=replace(frames[0].exposure, duration=0.123))
    changed = replace(result, frames=(changed_frame, frames[1], frames[2]))
    again = unmix_collection(
      (changed,),
      (dye,),
      reporter="GREEN",
      passive_reference=None,
      enabled_filters=(result.filter,),
    )
    self.assertNotEqual(measurements[0].source.source_id, again[0].source.source_id)
    with self.assertRaises(TypeError):
      unmix_collection(
        (result, self.saved_results()[0]), (dye,), reporter="GREEN", passive_reference=None
      )
