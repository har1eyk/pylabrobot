"""The native three-exposure selection stage, separate from image calibration."""

import math
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ExposureSelection:
  """Select one already processed well value using the vendor's saturation decisions.

  Exposures are in seconds, with zero denoting a disabled exposure. Values must already have
  undergone the appropriate native extraction, background and exposure corrections. This class
  does not perform those stages or determine saturation from an image. It preserves the three
  inputs and exposes the selected index even when all exposures are flagged.
  """

  exposures: Tuple[float, float, float]
  values: Tuple[int, int, int]
  saturated: Tuple[bool, bool, bool]

  def __post_init__(self) -> None:
    """Require immutable triplets and values within the native unsigned storage range."""
    if any(
      type(group) is not tuple or len(group) != 3
      for group in (self.exposures, self.values, self.saturated)
    ):
      raise ValueError("Exposure selection requires three immutable triplets")
    if any(
      type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2147483.647
      for value in self.exposures
    ):
      raise ValueError("Exposures must be finite, nonnegative native-range seconds")
    if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in self.values):
      raise ValueError("Processed values must be unsigned 32-bit integers")
    if any(type(value) is not bool for value in self.saturated):
      raise ValueError("Saturation flags must be bool")

  @property
  def selected_index(self) -> int:
    """Return the native zero-based choice, including its single-exposure and tie behavior.

    A sole positive exposure is selected even when flagged. Otherwise use the uniquely longest
    unflagged exposure. Without a unique maximum, use the uniquely shortest original exposure;
    if that also ties, use index zero. An all-flagged selection is not evidence of usable data.
    """
    positive = [index for index, value in enumerate(self.exposures) if value > 0]
    if len(positive) == 1:
      return positive[0]
    candidates = [0 if flag else value for value, flag in zip(self.exposures, self.saturated)]
    longest = max(candidates)
    if candidates.count(longest) == 1:
      return candidates.index(longest)
    shortest = min(self.exposures)
    if self.exposures.count(shortest) == 1:
      return self.exposures.index(shortest)
    return 0

  @property
  def value(self) -> int:
    """Return the selected input without averaging, clipping or relabeling it as fluorescence."""
    return self.values[self.selected_index]
