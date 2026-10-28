"""Status-scheduled optical collection with durable identities and explicit processing inputs."""

import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from .acquisition import CameraAcquisition, CameraExposure, OpticalState
from .calibration import UniformityCalibration
from .fluorescence import BackgroundCalibration, CameraFilterResult, FluorescenceFrame
from .image_processing import PlatePixelMask
from .status import ABI7500Status
from .thermal_program import ThermalProgram, _integer


@dataclass(frozen=True)
class CollectionFilter:
  """A physical filter and exposures in seconds, in acquisition order.

  ``bracket`` requires three distinct exposures for ordinary fluorescence processing.
  ``fixed`` requires one or sixteen identical exposures for calibration-image averaging.
  ``automatic`` uses one or sixteen 2.0-second ceiling placeholders; the final duration is
  selected by a separate, journaled exposure search using the plan's saved ROI masks.
  ``background`` collects 2.0, 1.0 and 0.5 seconds in that order, with one or sixteen
  consecutive images at each duration. It retains the full background-fit inputs.
  The count is caller-specified; automatic frame-count selection is not inferred.
  The explicit mode prevents repeated exposures from silently becoming a PCR bracket.
  """

  position: int
  exposures: tuple[float, ...]
  mode: Literal["bracket", "fixed", "automatic", "background"] = "bracket"

  def __post_init__(self) -> None:
    """Require a supported bracket or a complete fixed-exposure calibration collection."""
    _integer("position", self.position, 1, 5)
    if self.mode not in ("bracket", "fixed", "automatic", "background"):
      raise ValueError("Unknown collection mode")
    counts = (
      (3, 18, 33, 48)
      if self.mode == "background"
      else ((3,) if self.mode == "bracket" else (1, 16))
    )
    if type(self.exposures) is not tuple or len(self.exposures) not in counts:
      raise ValueError(f"Collection mode {self.mode} requires an exposure count in {counts}")
    times = tuple(CameraExposure(value).milliseconds for value in self.exposures)
    if self.mode == "bracket" and len(set(times)) != 3:
      raise ValueError("Collection exposures must encode distinct millisecond values")
    if self.mode == "fixed" and len(set(self.exposures)) != 1:
      raise ValueError("Fixed collection exposures must be identical")
    if self.mode == "automatic" and any(value != 2.0 for value in self.exposures):
      raise ValueError("Automatic calibration requires the captured 2-second search ceiling")
    if self.mode == "background":
      repeats = tuple(times.count(duration) for duration in (2000, 1000, 500))
      expected = tuple(t for t, count in zip((2000, 1000, 500), repeats) for _ in range(count))
      if any(count not in (1, 16) for count in repeats) or times != expected:
        raise ValueError(
          "Background collection requires ordered 2, 1, 0.5-second groups of 1 or 16"
        )


@dataclass(frozen=True, order=True)
class CollectionPoint:
  """One expected hold: zero-based cycle-group/step indices and one-based repetition.

  Field order follows execution order, so points sort by group, repetition, then step.
  """

  cycle_index: int
  repetition: int
  step_index: int

  def __post_init__(self) -> None:
    """Validate the native finite-program index ranges."""
    _integer("cycle_index", self.cycle_index, 0, 47)
    _integer("repetition", self.repetition, 1, 99)
    _integer("step_index", self.step_index, 0, 6)

  def matches(self, status: ABI7500Status) -> bool:
    """Require holding at exactly this point; ramping and stale idle indices are insufficient."""
    return (
      status.state_code == 4
      and status.phase == "holding"
      and (status.cycle_index, status.repetition, status.step_index)
      == (self.cycle_index, self.repetition, self.step_index)
      and status.remaining_hold_time is not None
      and status.remaining_hold_time > 0
    )

  def collection_id(self, run_id: str) -> str:
    """Derive a stable identity that cannot change on a polling retry."""
    return f"{run_id}-c{self.cycle_index}-r{self.repetition}-s{self.step_index}"


@dataclass(frozen=True)
class CollectionCadence:
  """Fixed-image start offsets in seconds, grouped in physical-filter order.

  Offsets are relative to the first exposure's host CC submission. A monotonic clock avoids
  accumulating delays between images. Missing a target by more than ``max_lateness`` fails
  the collection before another CC; the driver never catches up by replaying exposures.
  This explicitly selects observations around each filter burst, not each individual frame.
  The instrument still owns the hold timing. Host scheduling is not a hardware trigger.
  """

  offsets: tuple[tuple[float, ...], ...]
  max_lateness: float = 0.5

  def __post_init__(self) -> None:
    """Require finite, increasing offsets and a bounded lateness tolerance."""
    if (
      isinstance(self.max_lateness, bool)
      or not math.isfinite(self.max_lateness)
      or not 0 < self.max_lateness <= 1
    ):
      raise ValueError("Cadence max_lateness must be positive and at most one second")
    if type(self.offsets) is not tuple or not 1 <= len(self.offsets) <= 5:
      raise ValueError("Cadence requires one to five immutable filter groups")
    flat = []
    for group in self.offsets:
      if type(group) is not tuple or len(group) not in (1, 16):
        raise ValueError("Cadence requires one or sixteen offsets per filter")
      for value in group:
        if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value < 5939:
          raise ValueError("Cadence offsets must be finite nonnegative seconds")
        flat.append(value)
    if flat[0] != 0 or any(a >= b for a, b in zip(flat, flat[1:])):
      raise ValueError("Cadence must start at zero and strictly increase across all filters")


@dataclass(frozen=True)
class CollectionPlan:
  """Explicit optical reads during selected holds, while thermal timing stays on the instrument.

  ``steps`` contains zero-based (cycle-group, step) pairs, applied to every repetition. The
  scheduler starts when remaining hold seconds enter ``read_window`` (default 45 seconds).
  A delay of more than two reported seconds misses the window and fails closed. This budget
  must accommodate all filter moves, exposure transfers and status queries on the control host;
  it is not a hardware trigger or a guarantee of subsecond synchronization. No hold is extended.
  The lamp must already be enabled at native power 1; this plan never implicitly changes it.
  ``park_between_collections`` moves to absolute position 0 after each complete burst, including
  the last, and verifies stationary readback with the lamp still on. It does not home the wheel.
  The read window must also accommodate this move. Disabled by default for compatibility with
  existing acquisition plans; optical extinction is an instrument-specific validation result.
  ``home_after_collections`` selects one-based collection ordinals that use native asynchronous
  homing instead of an absolute parking move. It requires parking enabled; other collections
  still park normally. Homing intent belongs to the durable collection identity and is never
  replayed after failure. This is not recalibration or a lamp-counter reset.
  ``exposure_search_masks`` pins original ROI bytes for every automatic filter. All searches
  finish before final images begin. Up to eleven probes per filter are saved separately from
  final acquisitions. The minimum budget includes 22 exposure seconds per automatic filter,
  but USB transfers, processing and filter movement require additional caller-provided margin.
  ``park_after_exposure_search`` closes the light path at absolute filter 0 after all probes,
  before moving to the first final-image filter, as in the captured pure-dye routine.
  ``cadence`` opts fixed calibration images into scheduled filter bursts. Fresh observations
  bracket each burst; intermediate state changes cannot be observed. Each image remains
  uncertain until the burst's final observations pass. Ordinary plans retain per-frame checks.
  """

  steps: tuple[tuple[int, int], ...]
  filters: tuple[CollectionFilter, ...]
  read_window: int = 45
  park_between_collections: bool = False
  home_after_collections: tuple[int, ...] = ()
  exposure_search_masks: tuple[PlatePixelMask, ...] = ()
  park_after_exposure_search: bool = False
  cadence: Optional[CollectionCadence] = None

  def __post_init__(self) -> None:
    """Reject empty, duplicate and unordered settings before any instrument I/O."""
    _integer("read_window", self.read_window, 5, 5939)
    if type(self.park_between_collections) is not bool:
      raise ValueError("park_between_collections must be a boolean")
    if type(self.home_after_collections) is not tuple:
      raise ValueError("home_after_collections must be an immutable tuple")
    for ordinal in self.home_after_collections:
      _integer("home_after_collections ordinal", ordinal, 1, 32760)
    if self.home_after_collections != tuple(sorted(set(self.home_after_collections))):
      raise ValueError("Homing ordinals must be unique and in execution order")
    if self.home_after_collections and not self.park_between_collections:
      raise ValueError("Homing requires park_between_collections=True")
    if type(self.steps) is not tuple or not self.steps:
      raise ValueError("Collection steps must be a nonempty immutable tuple")
    for pair in self.steps:
      if type(pair) is not tuple or len(pair) != 2:
        raise ValueError("Each collection step must be a (cycle_index, step_index) tuple")
      CollectionPoint(pair[0], 1, pair[1])
    if self.steps != tuple(sorted(set(self.steps))):
      raise ValueError("Collection steps must be unique and in execution order")
    if type(self.filters) is not tuple or not 1 <= len(self.filters) <= 5:
      raise ValueError("Select one to five immutable filter settings")
    if any(not isinstance(value, CollectionFilter) for value in self.filters):
      raise ValueError("Filter settings must be CollectionFilter objects")
    positions = tuple(value.position for value in self.filters)
    if positions != tuple(sorted(set(positions))):
      raise ValueError("Physical filters must be unique and in ascending order")
    automatic = tuple(f.position for f in self.filters if f.mode == "automatic")
    if type(self.park_after_exposure_search) is not bool:
      raise ValueError("park_after_exposure_search must be a boolean")
    if self.park_after_exposure_search and not automatic:
      raise ValueError("Parking after exposure search requires automatic filters")
    if (
      type(self.exposure_search_masks) is not tuple
      or tuple(mask.filter for mask in self.exposure_search_masks) != automatic
    ):
      raise ValueError("Automatic filters require matching masks in physical-filter order")
    if (
      sum(sum(value.exposures) for value in self.filters) + 22 * len(automatic)
      >= self.read_window - 2
    ):
      raise ValueError("Read window cannot fit even the requested exposures")
    if self.cadence is not None:
      if not isinstance(self.cadence, CollectionCadence):
        raise TypeError("cadence must be a CollectionCadence")
      if any(f.mode != "fixed" for f in self.filters) or len(self.cadence.offsets) != len(
        self.filters
      ):
        raise ValueError("Cadence requires matching fixed-exposure filter groups")
      starts: list[float] = []
      durations: list[float] = []
      for setting, offsets in zip(self.filters, self.cadence.offsets):
        if len(setting.exposures) != len(offsets):
          raise ValueError("Cadence must specify every fixed exposure exactly once")
        starts.extend(offsets)
        durations.extend(setting.exposures)
      if any(a + duration > b for a, b, duration in zip(starts, starts[1:], durations)):
        raise ValueError("Cadence cannot overlap exposures")
      if starts[-1] + durations[-1] + self.cadence.max_lateness >= self.read_window - 2:
        raise ValueError("Read window cannot fit the scheduled cadence")

  def probe_ids(self, collection_id: str) -> tuple[str, ...]:
    """Bounded probe slots, separate from the reserved final-image identities."""
    return tuple(
      f"{collection_id}-f{f.position}-p{index}"
      for f in self.filters
      if f.mode == "automatic"
      for index in range(11)
    )

  def points(self, program: ThermalProgram) -> tuple[CollectionPoint, ...]:
    """Expand selected steps, requiring the full collection window in every effective hold."""
    result = []
    for cycle_index, step_index in self.steps:
      if cycle_index >= len(program.cycles) or step_index >= len(program.cycles[cycle_index].steps):
        raise ValueError("Collection step does not exist in the thermal program")
      cycle = program.cycles[cycle_index]
      step = cycle.steps[step_index]
      for repetition in range(1, cycle.repetitions + 1):
        hold = step.hold_time + (repetition - 1) * step.hold_time_increment
        if hold < self.read_window:
          raise ValueError("Every selected hold must contain the entire collection window")
        result.append(CollectionPoint(cycle_index, repetition, step_index))
    if self.home_after_collections and self.home_after_collections[-1] > len(result):
      raise ValueError("Homing ordinal does not exist in the collection plan")
    return tuple(sorted(result))

  def to_dict(self) -> dict[str, Any]:
    """Serialize immutable settings for comparison after restart."""
    return {
      "steps": [list(pair) for pair in self.steps],
      "filters": [
        {"position": f.position, "exposures": list(f.exposures)}
        | ({"mode": f.mode} if f.mode != "bracket" else {})
        for f in self.filters
      ],
      "read_window": self.read_window,
      "park_between_collections": self.park_between_collections,
      "home_after_collections": list(self.home_after_collections),
      **(
        {
          "cadence": {
            "offsets": [list(g) for g in self.cadence.offsets],
            "max_lateness": self.cadence.max_lateness,
          }
        }
        if self.cadence is not None
        else {}
      ),
      **({"park_after_exposure_search": True} if self.park_after_exposure_search else {}),
      **(
        {
          "exposure_search_masks": [
            {"filter": mask.filter, "source_hex": mask.source_bytes.hex()}
            for mask in self.exposure_search_masks
          ]
        }
        if self.exposure_search_masks
        else {}
      ),
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, Any]) -> "CollectionPlan":
    """Restore a plan; older records explicitly retain their no-parking behavior."""
    return cls(
      tuple((pair[0], pair[1]) for pair in value["steps"]),
      tuple(
        CollectionFilter(f["position"], tuple(f["exposures"]), f.get("mode", "bracket"))
        for f in value["filters"]
      ),
      value["read_window"],
      value.get("park_between_collections", False),
      tuple(value.get("home_after_collections", ())),
      tuple(
        PlatePixelMask(bytes.fromhex(m["source_hex"]), m["filter"])
        for m in value.get("exposure_search_masks", ())
      ),
      value.get("park_after_exposure_search", False),
      None
      if value.get("cadence") is None
      else CollectionCadence(
        tuple(tuple(g) for g in value["cadence"]["offsets"]), value["cadence"]["max_lateness"]
      ),
    )


@dataclass(frozen=True)
class CollectionRecord:
  """A reserved optical read; an uncertain record is never resumed or repeated automatically."""

  run_id: str
  point: CollectionPoint
  ordinal: int
  before: ABI7500Status
  acquisition_ids: tuple[str, ...]
  state: Literal["uncertain", "complete"] = "uncertain"
  after: Optional[ABI7500Status] = None
  error: Optional[str] = None
  parked_optics: Optional[OpticalState] = None
  homing_requested: bool = False

  @property
  def collection_id(self) -> str:
    """Stable identifier shared by the checkpoint and its individual camera intents."""
    return self.point.collection_id(self.run_id)

  def __post_init__(self) -> None:
    """Require complete identity and phase evidence before accepting a completed checkpoint."""
    _integer("ordinal", self.ordinal, 1, 32760)
    if type(self.homing_requested) is not bool:
      raise ValueError("homing_requested must be a boolean")
    if self.state not in ("uncertain", "complete"):
      raise ValueError("Invalid collection state")
    if not self.point.matches(self.before):
      raise ValueError("Collection must begin within its designated hold")
    if type(self.acquisition_ids) is not tuple or not self.acquisition_ids:
      raise ValueError("Collection requires immutable acquisition identities")
    if len(set(self.acquisition_ids)) != len(self.acquisition_ids):
      raise ValueError("Duplicate collection acquisition identity")
    if self.parked_optics is not None and (
      not self.parked_optics.is_stationary
      or self.parked_optics.filter_position != 0
      or not self.parked_optics.lamp_enabled
      or self.parked_optics.lamp_power != 1
    ):
      raise ValueError("Parked optics require stationary position 0 with lamp power 1")
    if self.state == "complete" and (
      self.after is None
      or not self.point.matches(self.after)
      or self.before.run_start_record != self.after.run_start_record
      or self.error is not None
      or (self.homing_requested and self.parked_optics is None)
    ):
      raise ValueError("Completed collection requires the same run and hold at both boundaries")

  def to_dict(self) -> dict[str, Any]:
    """Preserve the original status payloads and every reserved exposure identity."""
    return {
      "run_id": self.run_id,
      "point": [self.point.cycle_index, self.point.repetition, self.point.step_index],
      "ordinal": self.ordinal,
      "before": self.before.raw_payload.hex(),
      "acquisition_ids": list(self.acquisition_ids),
      "state": self.state,
      "after": None if self.after is None else self.after.raw_payload.hex(),
      "error": self.error,
      "parked_optics": None if self.parked_optics is None else self.parked_optics.to_dict(),
      "homing_requested": self.homing_requested,
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, Any]) -> "CollectionRecord":
    """Restore a checkpoint without initiating thermal or optical operations."""
    return cls(
      value["run_id"],
      CollectionPoint(*value["point"]),
      value["ordinal"],
      ABI7500Status(bytes.fromhex(value["before"])),
      tuple(value["acquisition_ids"]),
      value["state"],
      None if value["after"] is None else ABI7500Status(bytes.fromhex(value["after"])),
      value["error"],
      None
      if value.get("parked_optics") is None
      else OpticalState.from_dict(value["parked_optics"]),
      value.get("homing_requested", False),
    )


@dataclass(frozen=True)
class FluorescenceCollection:
  """Final images and separately retained complete/partial automatic-exposure probes.

  ``acquisitions`` contains only planned final images. ``exposure_probes`` is the ordered
  search history, including uncertain frames; never include it in a calibration average.
  """

  record: CollectionRecord
  acquisitions: tuple[CameraAcquisition, ...]
  exposure_probes: tuple[CameraAcquisition, ...] = ()

  def filter_results(
    self,
    background: BackgroundCalibration,
    uniformity: UniformityCalibration,
    masks: tuple[PlatePixelMask, ...],
  ) -> tuple[CameraFilterResult, ...]:
    """Process a complete collection with explicitly associated calibration files.

    Stage is cycle-group + 1, cycle is its one-based repetition, and collection point is the
    plan's one-based ordinal. Physical filter positions are converted to saved indices by the
    camera processing API. Saturated fallback values retain their flags.
    """
    if (
      self.record.state != "complete"
      or tuple(a.acquisition_id for a in self.acquisitions) != self.record.acquisition_ids
    ):
      raise ValueError("Only a complete, ordered collection can be processed")
    by_filter = {mask.filter: mask for mask in masks}
    if len(by_filter) != len(masks):
      raise ValueError("Duplicate physical-filter masks")
    results = []
    for index in range(0, len(self.acquisitions), 3):
      acquisitions = self.acquisitions[index : index + 3]
      if len(acquisitions) != 3:
        raise ValueError("Each filter requires three acquisitions")
      frames = []
      for acquisition in acquisitions:
        frame = acquisition.frame
        if acquisition.state != "complete" or frame is None:
          raise ValueError("A complete original image is required")
        if (
          acquisition.thermal_after is None
          or not self.record.point.matches(acquisition.thermal_before)
          or not self.record.point.matches(acquisition.thermal_after)
          or acquisition.thermal_before.run_start_record != self.record.before.run_start_record
          or acquisition.thermal_after.run_start_record != self.record.before.run_start_record
          or acquisition.optics_before != acquisition.optics_after
          or not acquisition.optics_before.is_stationary
          or not acquisition.optics_before.lamp_enabled
          or acquisition.optics_before.lamp_power != 1
        ):
          raise ValueError("Acquisition observations do not match the collection hold and optics")
        position = acquisition.optics_before.filter_position
        if position not in by_filter:
          raise ValueError(f"Missing explicitly associated mask for physical filter {position}")
        frames.append(FluorescenceFrame(frame, acquisition.exposure, by_filter[position]))
      results.append(
        CameraFilterResult(
          (frames[0], frames[1], frames[2]),
          background,
          uniformity,
          self.record.point.cycle_index + 1,
          self.record.point.repetition,
          self.record.ordinal,
        )
      )
    return tuple(results)
