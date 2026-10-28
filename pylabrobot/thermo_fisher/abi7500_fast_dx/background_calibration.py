"""Fit native background coefficients from complete water-plate images, without hardware I/O.

The recovered path uses three fixed exposures, OPB=0 ring subtraction and a binary32 linear
fit. Its saturation/threshold screen is not the vendor validity or compensation workflow.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .calibration import _float32
from .fluorescence import BackgroundCalibration, ExtractedWell, FluorescenceFrame

_EXPOSURES_MS = (2000, 1000, 500)


def _fit_background(raw: tuple[int, int, int]) -> tuple[float, float]:
  """Return binary32 offset/slope, preserving native timing and uint32 normalization.

  Raw values are averaged, ring-subtracted integers at 2000, 1000 and 500 ms. The fit uses
  requested milliseconds as x; y first passes through effective-integration normalization,
  uint32 truncation and a binary32 store. The final fit accumulates in binary64.
  """
  x = _EXPOSURES_MS
  y = []
  for value, exposure in zip(raw, x):
    effective = math.ceil((exposure + 0.2746) / 0.1702) * 0.1702
    normalized = min(0xFFFFFFFF, max(0, int(1000 * value / effective)))
    y.append(_float32(normalized) * exposure / 1000)
  sx, sy = sum(x), sum(y)
  mean = sx / 3
  numerator = denominator = 0.0
  for exposure, fitted_value in zip(x, y):
    delta = exposure - mean
    denominator += delta * delta
    numerator += delta * fitted_value
  slope = numerator / denominator
  return _float32((sy - sx * slope) / 3), _float32(slope)


@dataclass(frozen=True)
class BackgroundCalibrationGroup:
  """One complete fixed-exposure collection in acquisition order.

  Args:
    frames: Original camera frames sharing a mask and exposure of 2, 1 or 0.5 seconds.
    expected_frame_count: Independently recorded planned length, either 1 or 16. Do not
      derive this from an interrupted collection's available images.

  Saturated images remain available for diagnostics and prevent calibration export.
  The caller must exclude startup/search frames and uncertain acquisitions.
  """

  frames: tuple[FluorescenceFrame, ...]
  expected_frame_count: int

  def __post_init__(self) -> None:
    """Reject partial collections and settings outside the recovered background path."""
    if type(self.expected_frame_count) is not int or self.expected_frame_count not in (1, 16):
      raise ValueError("Background calibration requires an expected count of 1 or 16 frames")
    if not isinstance(self.frames, tuple) or len(self.frames) != self.expected_frame_count:
      raise ValueError("Incomplete background calibration collection")
    if any(not isinstance(frame, FluorescenceFrame) for frame in self.frames):
      raise TypeError("Background calibration requires FluorescenceFrame inputs")
    first = self.frames[0]
    if first.exposure.milliseconds not in _EXPOSURES_MS:
      raise ValueError("Background calibration requires exposures of 2, 1 and 0.5 seconds")
    if any(frame.mask != first.mask or frame.exposure != first.exposure for frame in self.frames):
      raise ValueError("Background frames must share the same mask, filter and exposure")

  @cached_property
  def _extracted(self) -> tuple[tuple[ExtractedWell, ...], ...]:
    """Extract each retained image once, including its original ring and clipping statistics."""
    return tuple(frame.wells for frame in self.frames)

  @cached_property
  def raw_values(self) -> tuple[int, ...]:
    """Average untruncated ring-subtracted sums, then clip below zero and truncate once."""
    values = []
    for index in range(96):
      total = 0.0
      for wells in self._extracted:
        well = wells[index]
        total += well.pixel_sum - well.ring_sum / well.ring_count * well.pixel_count
      values.append(max(0, int(total / len(self.frames))))
    return tuple(values)

  @cached_property
  def saturated_wells(self) -> tuple[int, ...]:
    """Return one-based wells saturated in any image, using the native >6 pixels >4000 rule."""
    return tuple(
      index + 1 for index in range(96) if any(wells[index].saturated for wells in self._extracted)
    )


@dataclass(frozen=True)
class BackgroundCalibrationFilter:
  """Three complete exposure groups for one physical filter, using one identical ROI mask."""

  groups: tuple[BackgroundCalibrationGroup, ...]

  def __post_init__(self) -> None:
    """Require each exposure once and preserve frame order within every group."""
    if (
      not isinstance(self.groups, tuple)
      or len(self.groups) != 3
      or any(not isinstance(group, BackgroundCalibrationGroup) for group in self.groups)
    ):
      raise ValueError("Supply three background exposure groups")
    if {group.frames[0].exposure.milliseconds for group in self.groups} != set(_EXPOSURES_MS):
      raise ValueError("Supply exactly one background group at each of 2, 1 and 0.5 seconds")
    if any(group.frames[0].mask != self.groups[0].frames[0].mask for group in self.groups):
      raise ValueError("Background groups must share the same mask and filter")

  @property
  def physical_filter(self) -> int:
    """Physical wheel position 1–5, not the reversed saved channel."""
    return self.groups[0].frames[0].mask.filter

  @property
  def saved_filter(self) -> int:
    """Saved file channel 1–5 (A–E), the reverse of physical filter order."""
    return 6 - self.physical_filter

  @property
  def ordered_groups(self) -> tuple[BackgroundCalibrationGroup, ...]:
    """Return groups in native descending exposure order, keeping each image sequence intact."""
    return tuple(sorted(self.groups, key=lambda group: -group.frames[0].exposure.milliseconds))

  @cached_property
  def coefficients(self) -> tuple[tuple[float, float], ...]:
    """Return 96 (offset, slope) pairs in row-major A1–H12 order, including signed offsets."""
    long, middle, short = (group.raw_values for group in self.ordered_groups)
    return tuple(_fit_background((long[i], middle[i], short[i])) for i in range(96))

  @property
  def threshold(self) -> float:
    """Reference 1000 ms screen limit: 90,000 FSU for saved E, 72,000 for saved A–D."""
    return 90000.0 if self.saved_filter == 5 else 72000.0

  @property
  def signals(self) -> tuple[float, ...]:
    """Binary32 fitted background at the reference 1000 ms virtual exposure, in FSU."""
    return tuple(_float32(offset + _float32(1000 * slope)) for offset, slope in self.coefficients)

  @property
  def above_threshold(self) -> tuple[int, ...]:
    """One-based wells strictly above the recorded reference limit; no replacement is applied."""
    return tuple(i + 1 for i, value in enumerate(self.signals) if value > self.threshold)


@dataclass(frozen=True)
class BackgroundCalibrationResult:
  """A five-filter fit with original images, masks and caller-declared acquisition identities.

  No previous background coefficients are inputs. A passing result means only that complete
  data passed the recovered saturation and threshold checks, not current calibration validity.
  Keep the JSON report beside the exported coefficients to preserve their association.
  """

  filters: tuple[BackgroundCalibrationFilter, ...]
  instrument_id: str
  scanner_id: str
  timestamp: int

  def __post_init__(self) -> None:
    """Require all five physical filters and explicit bounded acquisition provenance."""
    if (
      not isinstance(self.filters, tuple)
      or len(self.filters) != 5
      or any(not isinstance(item, BackgroundCalibrationFilter) for item in self.filters)
      or {item.physical_filter for item in self.filters} != set(range(1, 6))
    ):
      raise ValueError("Supply exactly one background collection for each physical filter 1–5")
    for identity in (self.instrument_id, self.scanner_id):
      if not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identity) is None:
        raise ValueError("Supply explicit instrument and scanner identifiers")
    if type(self.timestamp) is not int or self.timestamp < 0:
      raise ValueError("Timestamp must be nonnegative integer milliseconds since the Unix epoch")

  @property
  def offsets(self) -> tuple[tuple[float, ...], ...]:
    """Binary32 offsets in saved A–E order, then row-major wells A1–H12."""
    return tuple(
      tuple(pair[0] for pair in item.coefficients)
      for item in sorted(self.filters, key=lambda entry: entry.saved_filter)
    )

  @property
  def slopes(self) -> tuple[tuple[float, ...], ...]:
    """Binary32 slopes per requested millisecond, in saved A–E and row-major well order."""
    return tuple(
      tuple(pair[1] for pair in item.coefficients)
      for item in sorted(self.filters, key=lambda entry: entry.saved_filter)
    )

  @property
  def failure_reasons(self) -> tuple[str, ...]:
    """Report every saturated group and excessive fitted background without dropping images."""
    reasons = []
    for item in sorted(self.filters, key=lambda entry: entry.physical_filter):
      for group in item.ordered_groups:
        if group.saturated_wells:
          reasons.append(
            f"Physical filter {item.physical_filter}, {group.frames[0].exposure.milliseconds} ms: "
            f"saturated wells {group.saturated_wells}"
          )
      if item.above_threshold:
        reasons.append(
          f"Physical filter {item.physical_filter}: wells above background threshold "
          f"{item.above_threshold}"
        )
    return tuple(reasons)

  @property
  def passed(self) -> bool:
    """Whether these data pass the recovered screen; this is not vendor certification."""
    return not self.failure_reasons

  def to_background(self) -> BackgroundCalibration:
    """Export passing coefficients to the supported CSV layout with lossless binary32 precision.

    The version-5 layout has no provenance fields. Save :meth:`report` alongside these bytes;
    it includes their SHA-256 and the exact source masks and image hashes. This method neither
    writes files nor modifies vendor calibration, validity flags or instrument settings.
    """
    if self.failure_reasons:
      raise ValueError("Background calibration failed: " + "; ".join(self.failure_reasons))
    lines = [
      "Background Calibration, version = 5",
      "[Offsets] rows = Filters A B C D E: columns = wells",
    ]
    lines.extend(",".join(format(value, ".9g") for value in row) + "," for row in self.offsets)
    lines.append(
      "[slopes], from formula for water subtraction [(Dye-offset)*VE/exp] - "
      "[(slope*VE)]: Rows = Filters A B C D E: columns = wells"
    )
    lines.extend(",".join(format(value, ".9g") for value in row) + "," for row in self.slopes)
    return BackgroundCalibration(("\n".join(lines) + "\n").encode("utf-8"))

  def report(self) -> dict[str, Any]:
    """Return JSON-compatible provenance, fit inputs, coefficients and quality findings."""
    return {
      "schema": "plr-abi7500-background-calibration-1",
      "instrument_id": self.instrument_id,
      "scanner_id": self.scanner_id,
      "timestamp": self.timestamp,
      "passed": self.passed,
      "failure_reasons": self.failure_reasons,
      "background_sha256": self.to_background().sha256 if self.passed else None,
      "coefficient_order": "saved filters A–E, row-major wells A1–H12",
      "offsets": self.offsets,
      "slopes": self.slopes,
      "filters": [
        {
          "physical_filter": item.physical_filter,
          "saved_filter": item.saved_filter,
          "mask_sha256": item.groups[0].frames[0].mask.sha256,
          "threshold": item.threshold,
          "signals": item.signals,
          "above_threshold": item.above_threshold,
          "groups": [
            {
              "exposure_ms": group.frames[0].exposure.milliseconds,
              "expected_frame_count": group.expected_frame_count,
              "raw_values": group.raw_values,
              "saturated_wells": group.saturated_wells,
              "frame_sha256": [
                hashlib.sha256(f.source.wire_data).hexdigest() for f in group.frames
              ],
            }
            for group in item.ordered_groups
          ],
        }
        for item in sorted(self.filters, key=lambda entry: entry.physical_filter)
      ],
    }


def calculate_background_calibration(
  filters: tuple[BackgroundCalibrationFilter, ...],
  *,
  instrument_id: str,
  scanner_id: str,
  timestamp: int,
) -> BackgroundCalibrationResult:
  """Calculate background from water-plate images and matching ROI masks, entirely offline.

  Args:
    filters: Five complete physical-filter collections, each with 2, 1 and 0.5 second groups.
      Supply only confirmed images from the same acquisition and calibration setup.
    instrument_id: Instrument serial recorded during acquisition.
    scanner_id: Camera USB serial recorded during acquisition.
    timestamp: Acquisition timestamp in integer milliseconds since the Unix epoch.

  Returns:
    The retained inputs, fitted coefficients and quality screen. No previous calibration,
    uniformity correction, dye spectrum or vendor expected result enters the fit.
  """
  return BackgroundCalibrationResult(filters, instrument_id, scanner_id, timestamp)
