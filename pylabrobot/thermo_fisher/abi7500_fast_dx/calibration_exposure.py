"""Offline replay of v2.3 optical-uniformity and pure-dye exposure search; no camera or table writes."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .fluorescence import FluorescenceFrame
from .thermal_program import _integer


@dataclass(frozen=True)
class CalibrationExposureProbe:
  """One probe's exposure and counts of pixels above 4000 in each of 96 well masks.

  Saturation is assessed per well, not over the whole camera image. These search images
  are deliberately allowed to saturate and must never enter the final calibration average.
  """

  milliseconds: int
  saturation_counts: tuple[int, ...]

  def __post_init__(self) -> None:
    """Require bounded integer observations from all physical wells."""
    _integer("milliseconds", self.milliseconds, 10, 2000)
    if type(self.saturation_counts) is not tuple or len(self.saturation_counts) != 96:
      raise ValueError("Exposure search requires saturation counts for all 96 wells")
    for count in self.saturation_counts:
      _integer("saturation count", count, 0, 76800)

  @classmethod
  @lru_cache(maxsize=64)
  def from_frame(cls, frame: FluorescenceFrame) -> "CalibrationExposureProbe":
    """Cache bounded immutable image/mask results without skipping journal validation."""
    return cls(frame.exposure.milliseconds, tuple(w.saturation_count for w in frame.wells))


@dataclass(frozen=True)
class CalibrationExposureSearch:
  """Replay BudgieManager::OptimizeExposureTable with the captured 2000 ms ceiling.

  The lower bound is 10 ms; more than six pixels above 4000 in any well is saturated.
  Integer bisection stops at a one-ms half-interval. The selected safe bound is rounded
  down to 10 ms and multiplied by 0.9. An all-saturated search is rejected rather than
  issuing the vendor's possible zero-ms result. At most eleven probes are required.
  This reproduces exposure selection, not a guarantee that later frames cannot saturate.
  """

  probes: tuple[CalibrationExposureProbe, ...] = ()

  def _replay(self) -> tuple[Optional[int], Optional[int]]:
    """Return the next probe or final reduced exposure, rejecting gaps and extra observations."""
    lower, upper, current, safe, precision = 10, 2000, 2000, 0, 10
    done = False
    for probe in self.probes:
      if not isinstance(probe, CalibrationExposureProbe) or done or probe.milliseconds != current:
        raise ValueError("Probe exposure disagrees with the search history")
      if max(probe.saturation_counts) > 6:
        upper = current
        current = lower + (current - lower) // 2
        span = current - lower
      else:
        lower = safe = current
        current += (upper - current) // 2
        span = current - lower
      if span <= precision:
        precision = 1
      done = span <= precision or current <= 10
    if not done:
      return current, None
    selected = min(max(10, current), safe) // 10 * 9
    if selected < 5:
      raise ValueError("No usable unsaturated calibration exposure was found")
    return None, selected

  def __post_init__(self) -> None:
    """Validate the entire ordered history, including the requested exposure of each probe."""
    if type(self.probes) is not tuple or len(self.probes) > 11:
      raise ValueError("Exposure search requires at most eleven immutable probes")
    self._replay()

  @property
  def next_milliseconds(self) -> Optional[int]:
    """Requested duration for the next probe, or None once the search is complete."""
    return self._replay()[0]

  @property
  def selected_milliseconds(self) -> Optional[int]:
    """Reduced final exposure, available only after a complete usable search."""
    return self._replay()[1]

  def add(self, probe: CalibrationExposureProbe) -> "CalibrationExposureSearch":
    """Return an independently validated history; never mutate or replay an exposure."""
    return CalibrationExposureSearch(self.probes + (probe,))
