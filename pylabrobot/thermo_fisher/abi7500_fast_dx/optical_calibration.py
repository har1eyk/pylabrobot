"""Calculate optical uniformity from complete camera collections without instrument I/O.

This is the five-filter, 96-well, 12-bit C9254 processing path. Acquisition/search frames must
be separated by the caller: only final, fixed-exposure collections belong in the calculation.
The returned calibration describes the supplied data; it does not certify current instrument
condition, dye concentrations, or assay performance.
"""

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .calibration import UniformityCalibration, _float32
from .fluorescence import BackgroundCalibration, FluorescenceFrame

logger = logging.getLogger(__name__)


def _sum_float32(values: tuple[float, ...]) -> float:
  """Accumulate in input order with a binary32 store after each addition."""
  total = 0.0
  for value in values:
    total = _float32(total + value)
  return total


def _neighbors(well: int) -> tuple[int, ...]:
  """Return zero-based adjacent wells in row-major order, excluding the center."""
  row, column = divmod(well, 12)
  return tuple(
    r * 12 + c
    for r in range(max(0, row - 1), min(8, row + 2))
    for c in range(max(0, column - 1), min(12, column + 2))
    if (r, c) != (row, column)
  )


def _find_spectral_outliers(
  normalized: tuple[tuple[float, ...], ...], excluded: set[int]
) -> tuple[int, ...]:
  """Apply the native leave-one-out six-sigma test to normalized spectral distances.

  Inputs are wells by physical filter. Both spectral means and absolute-distance sums use
  binary32; threshold mean/sample variance use binary64 with a final binary32 threshold.
  Already excluded wells participate in neither the mean nor the threshold population.
  """
  distances = []
  for well, values in enumerate(normalized):
    others = tuple(i for i in range(96) if i != well and i not in excluded)
    average = tuple(
      _float32(_sum_float32(tuple(normalized[i][f] for i in others)) / len(others))
      for f in range(5)
    )
    distances.append(
      0.0
      if well in excluded
      else _sum_float32(tuple(abs(_float32(v - a)) for v, a in zip(values, average)))
    )
  flagged = []
  for well, distance in enumerate(distances):
    if well in excluded:
      continue
    population = tuple(v for i, v in enumerate(distances) if i != well and i not in excluded)
    total = squares = 0.0
    for value in population:
      total += value
      squares += value * value
    variance = max(0, (squares - total * total / len(population)) / (len(population) - 1))
    threshold = _float32(total / len(population) + 6 * math.sqrt(variance))
    if distance > threshold:
      flagged.append(well)
  return tuple(flagged)


@dataclass(frozen=True)
class OpticalCalibrationAnalysis:
  """Optical coefficients derived from five complete, background-corrected well arrays.

  ``spectra`` uses saved filter order 1–5 and row-major wells A1–H12. It contains the integer
  signals before optical correction. Normally construct this through
  :func:`calculate_optical_calibration`, which also extracts and preserves source images.
  Direct construction supports previously extracted calibration data and offline regression tests.
  Empty and spectral-outlier wells are reported separately. Up to four isolated wells can be
  replaced using at least two unflagged neighbors, following the recovered native rule.
  """

  spectra: tuple[tuple[int, ...], ...]
  background: BackgroundCalibration

  def __post_init__(self) -> None:
    """Require five immutable arrays of 96 uint32 signals and native background provenance."""
    if (
      not isinstance(self.spectra, tuple)
      or len(self.spectra) != 5
      or any(not isinstance(row, tuple) or len(row) != 96 for row in self.spectra)
      or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for row in self.spectra for v in row)
    ):
      raise ValueError("Optical spectra must be five tuples of 96 uint32 well signals")
    if not isinstance(self.background, BackgroundCalibration):
      raise TypeError("Optical calibration requires native BackgroundCalibration")

  @cached_property
  def empty_wells(self) -> tuple[int, ...]:
    """Return one-based indices whose strongest filter is below twice its background slope.

    The native virtual exposure is 1000 ms. Ties follow physical filter order, the reverse
    of saved filter order. An all-zero spectrum is always considered empty.
    """
    slopes = self.background.slopes
    empty = []
    for well in range(96):
      values = tuple(_float32(self.spectra[f][well]) for f in range(5))
      strongest = max(range(4, -1, -1), key=lambda f: values[f])
      value = values[strongest]
      if value == 0 or value < 2000 * slopes[strongest][well]:
        empty.append(well + 1)
    return tuple(empty)

  @cached_property
  def outlier_wells(self) -> tuple[int, ...]:
    """Return one-based spectral outliers, including successive exclusion passes."""
    if len(self.empty_wells) >= 5:
      return ()
    normalized = []
    for well in range(96):
      values = tuple(_float32(self.spectra[f][well]) for f in range(4, -1, -1))
      maximum = max(values)
      normalized.append(tuple(_float32(v / maximum) if maximum else 0.0 for v in values))
    excluded = {well - 1 for well in self.empty_wells}
    outliers: list[int] = []
    while len(excluded) < 5:
      found = _find_spectral_outliers(tuple(normalized), excluded)
      if not found:
        break
      outliers.extend(well + 1 for well in found)
      excluded.update(found)
    return tuple(outliers)

  @property
  def flagged_wells(self) -> tuple[int, ...]:
    """Return one-based empty and outlier indices in detection order."""
    return self.empty_wells + self.outlier_wells

  @cached_property
  def failure_reasons(self) -> tuple[str, ...]:
    """Return reasons coefficients cannot be accepted, without discarding the source data."""
    flagged = {well - 1 for well in self.flagged_wells}
    if len(flagged) >= 5:
      return ("Five or more empty or spectral-outlier wells",)
    if any(len(set(_neighbors(well)) - flagged) < 2 for well in flagged):
      return ("A flagged well has fewer than two unflagged adjacent wells",)
    for row in self.spectra:
      if any(value == 0 for well, value in enumerate(row) if well not in flagged):
        return ("A retained well has a nonpositive filter signal",)
      if any(not any(row[i] > 0 for i in _neighbors(well) if i not in flagged) for well in flagged):
        return ("A replacement has a nonpositive filter signal",)
    return ()

  @property
  def passed(self) -> bool:
    """Whether these data satisfy the recovered calculation checks, not hardware validity."""
    return not self.failure_reasons

  @property
  def corrected_wells(self) -> tuple[int, ...]:
    """Return wells actually substituted; failed data are never silently repaired."""
    return self.flagged_wells if self.passed else ()

  @cached_property
  def corrected_spectra(self) -> tuple[tuple[float, ...], ...]:
    """Return binary32 signals with flagged wells replaced by unflagged neighbor means."""
    if self.failure_reasons:
      raise ValueError("Optical calibration failed: " + "; ".join(self.failure_reasons))
    flagged = {well - 1 for well in self.flagged_wells}
    corrected = []
    for original in self.spectra:
      row = [_float32(value) for value in original]
      for well in flagged:
        neighbors = tuple(i for i in _neighbors(well) if i not in flagged)
        row[well] = _float32(_sum_float32(tuple(row[i] for i in neighbors)) / len(neighbors))
      corrected.append(tuple(row))
    return tuple(corrected)

  @cached_property
  def coefficients(self) -> tuple[tuple[float, ...], ...]:
    """Return newly calculated binary32 mean/signal factors, before text serialization."""
    coefficients = []
    for row in self.corrected_spectra:
      mean = _float32(_sum_float32(row) / 96)
      coefficients.append(tuple(_float32(mean / value) for value in row))
    return tuple(coefficients)

  @property
  def coefficient_text(self) -> tuple[tuple[str, ...], ...]:
    """Return the vendor file's six-significant-digit representation of the factors."""
    return tuple(tuple(format(v, ".6g") for v in row) for row in self.coefficients)


def _calibration_spectra(
  frames: tuple[FluorescenceFrame, ...], background: BackgroundCalibration, saved_filter: int
) -> tuple[int, ...]:
  """Average ring-subtracted images, then apply native background and integration timing."""
  extracted = tuple(frame.wells for frame in frames)
  exposure_ms = frames[0].exposure.milliseconds
  effective_ms = math.ceil((exposure_ms + 0.2746) / 0.1702) * 0.1702
  offsets = background.offsets[saved_filter - 1]
  slopes = background.slopes[saved_filter - 1]
  values = []
  for index in range(96):
    total = 0.0
    for wells in extracted:
      well = wells[index]
      total += well.pixel_sum - well.ring_sum / well.ring_count * well.pixel_count
    raw = max(0, int(total / len(extracted)))
    value = 1000 * (raw - offsets[index]) / effective_ms - 1000 * slopes[index]
    values.append(int(min(0xFFFFFFFF, max(0, value))))
  return tuple(values)


@dataclass(frozen=True)
class OpticalCalibrationFilter:
  """One final fixed-exposure collection, retaining every original camera frame.

  Args:
    frames: Frames in acquisition order, with one physical filter, mask and exposure.
    expected_frame_count: Independently recorded collection length (1 or 16 in the reference
      session). Requiring this prevents accepting an interrupted collection as complete.
  """

  frames: tuple[FluorescenceFrame, ...]
  expected_frame_count: int

  def __post_init__(self) -> None:
    """Reject partial collections, mixed settings and saturated final images."""
    if type(self.expected_frame_count) is not int or self.expected_frame_count not in (1, 16):
      raise ValueError("Optical calibration requires an expected count of 1 or 16 frames")
    if not isinstance(self.frames, tuple) or len(self.frames) != self.expected_frame_count:
      raise ValueError("Incomplete optical calibration collection")
    first = self.frames[0]
    for frame in self.frames:
      if frame.mask != first.mask or frame.exposure != first.exposure:
        raise ValueError("Calibration frames must share the same mask, filter and exposure")
      if any(well.saturated for well in frame.wells):
        raise ValueError("Saturated final calibration image; repeat exposure selection")

  @property
  def physical_filter(self) -> int:
    """Return the physical wheel position, 1–5."""
    return self.frames[0].mask.filter

  @property
  def saved_filter(self) -> int:
    """Return the reversed vendor file channel, 1–5."""
    return 6 - self.physical_filter

  def spectra(self, background: BackgroundCalibration) -> tuple[int, ...]:
    """Average ring-subtracted signals before truncation and background normalization.

    These are the original, uncorrected uniformity spectra in row-major well order. No old
    uniformity coefficients enter this calculation. Integration uses the recovered camera
    timing formula, with the requested exposure retained on each frame.
    """
    return _calibration_spectra(self.frames, background, self.saved_filter)


@dataclass(frozen=True)
class OpticalCalibrationResult:
  """A reproducible raw-image calculation, its source calibration and declared identities.

  This object never opens USB, modifies the instrument, or installs a vendor-PC calibration.
  Identities and timestamp must describe the acquisition; the caller is responsible for
  associating the correct plate, masks and background with that acquisition.
  """

  filters: tuple[OpticalCalibrationFilter, ...]
  background: BackgroundCalibration
  instrument_id: str
  scanner_id: str
  timestamp: int

  def __post_init__(self) -> None:
    """Require exactly one collection per physical filter and bounded provenance fields."""
    if (
      not isinstance(self.filters, tuple)
      or len(self.filters) != 5
      or {item.physical_filter for item in self.filters} != set(range(1, 6))
    ):
      raise ValueError("Supply exactly one final collection for each physical filter 1–5")
    if not isinstance(self.background, BackgroundCalibration):
      raise TypeError("Optical calibration requires native BackgroundCalibration")
    for identity in (self.instrument_id, self.scanner_id):
      if not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identity) is None:
        raise ValueError("Supply explicit instrument and scanner identifiers")
    if type(self.timestamp) is not int or self.timestamp < 0:
      raise ValueError("Timestamp must be nonnegative integer milliseconds since the Unix epoch")

  @cached_property
  def analysis(self) -> OpticalCalibrationAnalysis:
    """Extract all five channels and assess the data without using old uniformity factors."""
    ordered = sorted(self.filters, key=lambda item: item.saved_filter)
    return OpticalCalibrationAnalysis(
      tuple(item.spectra(self.background) for item in ordered), self.background
    )

  def to_uniformity(self) -> UniformityCalibration:
    """Generate a new PLR-readable uniformity file only when the calculation passes.

    Six-significant-digit factors follow the vendor interchange format. Original integer
    spectra, before neighbor substitution, are included separately. The file's result flag
    records the computation outcome, not vendor certification or hardware validation.
    """
    factors = self.analysis.coefficient_text
    text = (
      "# Generated by PyLabRobot from camera data; acquisition control is not certified.\n"
      f"# Background SHA256: {self.background.sha256}\n"
    )
    for item in sorted(self.filters, key=lambda entry: entry.physical_filter):
      text += (
        f"# Physical filter {item.physical_filter} mask SHA256: {item.frames[0].mask.sha256}\n"
      )
    text += (
      f"[head]\nname=Calibration\nversion=1.0.0\noperator=PyLabRobot\ntimestamp={self.timestamp}\n"
      f"[instrument]\nid={self.instrument_id}\ntype=sds7500fast\n"
      f"[scanner]\nid={self.scanner_id}\nfilters=1,2,3,4,5\n"
      "[block]\ntype=96fast\nrows=8\ncolumns=12\n[result]\nresult=true\n"
    )
    for section, rows in (
      ("uniformity", factors),
      ("uniformity_spectra", tuple(tuple(str(v) for v in row) for row in self.analysis.spectra)),
    ):
      text += f"[{section}]\n"
      for index, values in enumerate(rows, 1):
        text += f"{index}=<<eof\n"
        text += "\n".join(",".join(values[start : start + 12]) for start in range(0, 96, 12))
        text += "\neof\n"
    return UniformityCalibration(text.encode("utf-8"))

  def report(self) -> dict[str, Any]:
    """Return JSON-compatible provenance, intermediate values and acceptance findings."""
    analysis = self.analysis
    return {
      "schema": "plr-abi7500-optical-calibration-1",
      "instrument_id": self.instrument_id,
      "scanner_id": self.scanner_id,
      "timestamp": self.timestamp,
      "background_sha256": self.background.sha256,
      "passed": analysis.passed,
      "failure_reasons": analysis.failure_reasons,
      "empty_wells": analysis.empty_wells,
      "outlier_wells": analysis.outlier_wells,
      "corrected_wells": analysis.corrected_wells,
      "spectra": analysis.spectra,
      "corrected_spectra": analysis.corrected_spectra if analysis.passed else None,
      "coefficients": analysis.coefficients if analysis.passed else None,
      "coefficient_text": analysis.coefficient_text if analysis.passed else None,
      "filters": [
        {
          "physical_filter": item.physical_filter,
          "saved_filter": item.saved_filter,
          "expected_frame_count": item.expected_frame_count,
          "exposure_ms": item.frames[0].exposure.milliseconds,
          "mask_sha256": item.frames[0].mask.sha256,
          "frame_sha256": [hashlib.sha256(f.source.wire_data).hexdigest() for f in item.frames],
        }
        for item in sorted(self.filters, key=lambda entry: entry.physical_filter)
      ],
    }


def calculate_optical_calibration(
  filters: tuple[OpticalCalibrationFilter, ...],
  background: BackgroundCalibration,
  *,
  instrument_id: str,
  scanner_id: str,
  timestamp: int,
) -> OpticalCalibrationResult:
  """Calculate new optical coefficients from five final camera collections, entirely offline.

  Supply a uniform mixed-dye plate's final fixed-exposure images, matching vendor ROI masks,
  and native background coefficients (prefer full-precision ``Calibration.sda``). Do not
  include startup or adaptive exposure-search frames.
  The result preserves inputs, reports quality failures and permits serialization only when
  those checks pass. No existing uniformity factors or expected vendor outputs are inputs.

  Args:
    filters: Exactly one complete final collection for each physical wheel position 1–5.
    background: Background from the same instrument and calibration setup.
    instrument_id: Instrument serial recorded during acquisition.
    scanner_id: Camera USB serial recorded during acquisition.
    timestamp: Acquisition timestamp in integer milliseconds since the Unix epoch.
  """
  result = OpticalCalibrationResult(filters, background, instrument_id, scanner_id, timestamp)
  logger.info(
    "Calculated optical calibration for %s: passed=%s, corrected wells=%s, failures=%s",
    instrument_id,
    result.analysis.passed,
    result.analysis.corrected_wells,
    result.analysis.failure_reasons,
  )
  return result
