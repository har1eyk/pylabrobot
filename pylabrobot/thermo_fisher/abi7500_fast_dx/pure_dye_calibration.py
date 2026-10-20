"""Calculate the v2.3 Java pure-dye spectrum from final images, entirely offline.

Native acquisition diagnostics and the Java saved-calibration calculation are distinct.
This module preserves their differing outlier decisions and never installs a calibration.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .calibration import _float32
from .fluorescence import BackgroundCalibration, FluorescenceFrame
from .optical_calibration import (
  OpticalCalibrationAnalysis,
  _calibration_spectra,
  _neighbors,
  _sum_float32,
)


def _sum_double(values: tuple[float, ...]) -> float:
  """Accumulate in Java loop order, without Python's compensated-sum implementation."""
  result = 0.0
  for value in values:
    result += value
  return result


def _java_outliers(
  normalized: tuple[tuple[float, ...], ...], excluded: set[int]
) -> tuple[int, ...]:
  """Apply one Java six-sigma pass: float means, double distances and sample deviation."""
  distances = []
  for well in range(96):
    others = tuple(i for i in range(96) if i != well and i not in excluded)
    if well in excluded or not others:
      distances.append(0.0)
      continue
    means = tuple(
      _float32(_sum_float32(tuple(row[i] for i in others)) / len(others)) for row in normalized
    )
    distances.append(
      _sum_double(tuple(abs(_float32(row[well] - mean)) for row, mean in zip(normalized, means)))
    )
  flagged = []
  for well, value in enumerate(distances):
    if well in excluded:
      continue
    population = tuple(v for i, v in enumerate(distances) if i != well and i not in excluded)
    mean = _sum_double(population) / len(population) if population else 0.0
    variance = (
      _sum_double(tuple((v - mean) * (v - mean) for v in population)) / (len(population) - 1)
      if len(population) > 1
      else 0.0
    )
    if value > mean + 6 * math.sqrt(variance):
      flagged.append(well + 1)
  return tuple(flagged)


@dataclass(frozen=True)
class PureDyeCalibrationAnalysis:
  """Five integer spectra and the saved Java calibration calculation, in saved channel order.

  Wells are row-major A1–H12. Quality indices are one-based. ``passed`` describes the recovered
  saved-calibration checks, not certification or absence of clipping. Native iterative outliers
  are reported separately because they need not equal the Java single-pass corrections.
  """

  spectra: tuple[tuple[int, ...], ...]
  background: BackgroundCalibration

  def __post_init__(self) -> None:
    """Validate bounded immutable spectra and an explicit background calibration."""
    OpticalCalibrationAnalysis(self.spectra, self.background)

  @cached_property
  def normalized_spectra(self) -> tuple[tuple[float, ...], ...]:
    """Normalize each well to its strongest channel with binary32 operations.

    All-zero wells use finite zeros until neighbor replacement; their empty flag is retained.
    This avoids propagating the vendor's 0/0 NaNs into serialized results.
    """
    converted = tuple(tuple(_float32(v) for v in row) for row in self.spectra)
    maxima = tuple(max(row[w] for row in converted) for w in range(96))
    return tuple(
      tuple(_float32(v / maxima[w]) if maxima[w] else 0.0 for w, v in enumerate(row))
      for row in converted
    )

  @cached_property
  def empty_wells(self) -> tuple[int, ...]:
    """Identify all-zero or below-twice-background wells, with saved-channel tie order."""
    slopes = self.background.slopes
    empty = []
    for well in range(96):
      values = tuple(_float32(row[well]) for row in self.spectra)
      strongest = max(range(5), key=lambda f: values[f])
      if values[strongest] == 0 or values[strongest] < _float32(2000 * slopes[strongest][well]):
        empty.append(well + 1)
    return tuple(empty)

  @cached_property
  def outlier_wells(self) -> tuple[int, ...]:
    """Return the Java single-pass spectral outliers without iterative reclassification."""
    if len(self.empty_wells) > 4:
      return ()
    return _java_outliers(self.normalized_spectra, {w - 1 for w in self.empty_wells})

  @property
  def flagged_wells(self) -> tuple[int, ...]:
    """Return empty and spectral-outlier wells in detection order."""
    return self.empty_wells + self.outlier_wells

  @cached_property
  def native_flagged_wells(self) -> tuple[int, ...]:
    """Report the native acquisition diagnostic's iterative decisions separately."""
    return OpticalCalibrationAnalysis(self.spectra, self.background).flagged_wells

  @cached_property
  def failure_reasons(self) -> tuple[str, ...]:
    """Reject excessive defects or insufficient neighbors before producing corrected output.

    PLR also limits total flagged wells to four, matching the native acquisition gate; the
    Java controller alone checks this limit only for empty wells.
    """
    flagged = {w - 1 for w in self.flagged_wells}
    if len(flagged) > 4:
      return ("More than four empty or spectral-outlier wells",)
    if any(len(set(_neighbors(w)) - flagged) < 2 for w in flagged):
      return ("A flagged well has fewer than two unflagged adjacent wells",)
    if len(self.native_flagged_wells) > 4:
      return ("Native iterative screening flags more than four wells",)
    return ()

  @property
  def passed(self) -> bool:
    """Whether the calculation passes its stated numerical checks."""
    return not self.failure_reasons

  @property
  def corrected_wells(self) -> tuple[int, ...]:
    """Return actual Java-style replacements; failed calculations produce none."""
    return self.flagged_wells if self.passed else ()

  @cached_property
  def corrected_spectra(self) -> tuple[tuple[float, ...], ...]:
    """Replace flagged normalized spectra using binary64 means, stored once as binary32.

    Zeros in off-peak channels are valid. These values describe relative dye spectra; no old
    optical-uniformity factors, inferred concentrations, multicomponent fit or Ct are applied.
    """
    if not self.passed:
      raise ValueError("Pure-dye calculation failed: " + "; ".join(self.failure_reasons))
    flagged = {w - 1 for w in self.flagged_wells}
    output = []
    for original in self.normalized_spectra:
      row = list(original)
      for well in flagged:
        neighbors = tuple(i for i in _neighbors(well) if i not in flagged)
        row[well] = _float32(_sum_double(tuple(original[i] for i in neighbors)) / len(neighbors))
      output.append(tuple(row))
    return tuple(output)


@dataclass(frozen=True)
class PureDyeCalibrationFilter:
  """One final fixed-exposure collection, preserving clipping rather than discarding images."""

  frames: tuple[FluorescenceFrame, ...]
  expected_frame_count: int

  def __post_init__(self) -> None:
    """Reject incomplete or mixed collections before any extraction."""
    if type(self.expected_frame_count) is not int or self.expected_frame_count not in (1, 16):
      raise ValueError("Pure-dye calibration requires an expected count of 1 or 16 frames")
    if not isinstance(self.frames, tuple) or len(self.frames) != self.expected_frame_count:
      raise ValueError("Incomplete pure-dye calibration collection")
    if any(not isinstance(frame, FluorescenceFrame) for frame in self.frames):
      raise TypeError("Supply FluorescenceFrame objects")
    first = self.frames[0]
    if any(frame.mask != first.mask or frame.exposure != first.exposure for frame in self.frames):
      raise ValueError("Calibration frames must share the same mask, filter and exposure")

  @property
  def physical_filter(self) -> int:
    """Return physical wheel position 1–5."""
    return self.frames[0].mask.filter

  @property
  def saved_filter(self) -> int:
    """Return reversed vendor file channel 1–5."""
    return 6 - self.physical_filter

  @cached_property
  def saturated_wells(self) -> tuple[int, ...]:
    """Return wells with more than six selected pixels above 4000 in any constituent frame."""
    return tuple(
      sorted({well.well for frame in self.frames for well in frame.wells if well.saturated})
    )

  def spectra(self, background: BackgroundCalibration) -> tuple[int, ...]:
    """Extract background-corrected uint32 signals at the native 1000 ms virtual exposure."""
    return _calibration_spectra(self.frames, background, self.saved_filter)


@dataclass(frozen=True)
class PureDyeCalibrationResult:
  """Reproducible five-filter pure-dye processing with raw frames and declared provenance."""

  filters: tuple[PureDyeCalibrationFilter, ...]
  background: BackgroundCalibration
  dye: str
  instrument_id: str
  scanner_id: str
  timestamp: int
  temperature: float

  def __post_init__(self) -> None:
    """Require complete filters, explicit identifiers and finite acquisition metadata."""
    if (
      not isinstance(self.filters, tuple)
      or len(self.filters) != 5
      or any(not isinstance(item, PureDyeCalibrationFilter) for item in self.filters)
      or {item.physical_filter for item in self.filters} != set(range(1, 6))
    ):
      raise ValueError("Supply one complete collection for each physical filter 1–5")
    if not isinstance(self.background, BackgroundCalibration):
      raise TypeError("Pure-dye calibration requires native BackgroundCalibration")
    for value in (self.dye, self.instrument_id, self.scanner_id):
      if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        raise ValueError("Supply bounded dye, instrument and scanner identifiers")
    if self.dye.lower() in {"head", "instrument", "scanner", "block", "plate", "puredyes"}:
      raise ValueError("Dye name conflicts with an interchange section")
    if type(self.timestamp) is not int or self.timestamp < 0:
      raise ValueError("Timestamp must be nonnegative integer milliseconds since the Unix epoch")
    if (
      isinstance(self.temperature, bool)
      or not math.isfinite(self.temperature)
      or not 4 <= self.temperature <= 99.9
    ):
      raise ValueError("Supply the acquisition temperature in degrees Celsius")

  @cached_property
  def analysis(self) -> PureDyeCalibrationAnalysis:
    """Extract and assess five complete channels without using expected calibration outputs."""
    return PureDyeCalibrationAnalysis(
      tuple(
        item.spectra(self.background) for item in sorted(self.filters, key=lambda f: f.saved_filter)
      ),
      self.background,
    )

  @property
  def retained_saturated_wells(self) -> tuple[int, ...]:
    """Return clipped wells that remain unreplaced; these need exposure review."""
    return tuple(
      sorted(
        {w for item in self.filters for w in item.saturated_wells}
        - set(self.analysis.corrected_wells)
      )
    )

  def to_ini(self) -> bytes:
    """Serialize one dye's new relative spectra without installing it or merging other dyes.

    A passed calculation with unreplaced clipping cannot be exported as an accepted calibration.
    Identity and source comments identify this as a PLR calculation, not a vendor certification.
    The report and original frames must be retained alongside this interchange file.
    """
    rows = self.analysis.corrected_spectra
    if self.retained_saturated_wells:
      raise ValueError("Unreplaced saturated wells require exposure review")
    text = (
      "# Calculated by PyLabRobot; retain the provenance report and original images.\n"
      f"# Background SHA256: {self.background.sha256}\n"
      f"[head]\nname=Calibration\nversion=1.0.0\noperator=PyLabRobot\ntimestamp={self.timestamp}\n"
      f"[instrument]\nid={self.instrument_id}\ntype=sds7500fast\n"
      f"[scanner]\nid={self.scanner_id}\nfilters=1,2,3,4,5\n[block]\n[plate]\nid=null\n"
      f"[puredyes]\ndyes={self.dye}\n[{self.dye}]\noperator=PyLabRobot\n"
      f"calibration-time={self.timestamp}\nexpiration-time=0\ntemperature={self.temperature}\nresult=true\n"
    )
    for index, values in enumerate(rows, 1):
      text += f"{index}=<<eof\n"
      text += "\n".join(
        ",".join(str(v) for v in values[start : start + 12]) for start in range(0, 96, 12)
      )
      text += "\neof\n"
    return text.encode()

  def report(self) -> dict[str, Any]:
    """Return JSON-compatible calculations, separate diagnostic decisions and frame hashes."""
    a = self.analysis
    return {
      "schema": "plr-abi7500-pure-dye-calibration-1",
      "dye": self.dye,
      "instrument_id": self.instrument_id,
      "scanner_id": self.scanner_id,
      "timestamp": self.timestamp,
      "temperature": self.temperature,
      "background_sha256": self.background.sha256,
      "passed": a.passed,
      "failure_reasons": a.failure_reasons,
      "empty_wells": a.empty_wells,
      "outlier_wells": a.outlier_wells,
      "native_flagged_wells": a.native_flagged_wells,
      "corrected_wells": a.corrected_wells,
      "spectra": a.spectra,
      "normalized_spectra": a.normalized_spectra,
      "corrected_spectra": a.corrected_spectra if a.passed else None,
      "retained_saturated_wells": self.retained_saturated_wells,
      "filters": [
        {
          "physical_filter": f.physical_filter,
          "saved_filter": f.saved_filter,
          "frame_count": len(f.frames),
          "exposure_ms": f.frames[0].exposure.milliseconds,
          "mask_sha256": f.frames[0].mask.sha256,
          "saturated_wells": f.saturated_wells,
          "frame_sha256": [
            hashlib.sha256(frame.source.wire_data).hexdigest() for frame in f.frames
          ],
        }
        for f in sorted(self.filters, key=lambda item: item.physical_filter)
      ],
    }


def calculate_pure_dye_calibration(
  filters: tuple[PureDyeCalibrationFilter, ...],
  background: BackgroundCalibration,
  *,
  dye: str,
  instrument_id: str,
  scanner_id: str,
  timestamp: int,
  temperature: float,
) -> PureDyeCalibrationResult:
  """Calculate one dye's relative spectrum from final camera collections, with no hardware I/O.

  Supply matching ROI masks and background from this instrument, a single-dye plate, and only
  final fixed-exposure frames. Saturated pixels remain flagged. This does not perform exposure
  selection, install vendor files, infer dye concentrations, or calculate Rn/Ct.
  """
  return PureDyeCalibrationResult(
    filters, background, dye, instrument_id, scanner_id, timestamp, temperature
  )
