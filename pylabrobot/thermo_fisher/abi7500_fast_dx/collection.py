"""Status-scheduled optical collection with durable identities and explicit processing inputs."""

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from .acquisition import CameraAcquisition, CameraExposure
from .calibration import UniformityCalibration
from .fluorescence import BackgroundCalibration, CameraFilterResult, FluorescenceFrame
from .image_processing import PlatePixelMask
from .status import ABI7500Status
from .thermal_program import ThermalProgram, _integer


@dataclass(frozen=True)
class CollectionFilter:
  """A physical filter position and three commanded exposures in seconds, in acquisition order."""

  position: int
  exposures: tuple[float, float, float]

  def __post_init__(self) -> None:
    """Require a real filter and three distinct supported camera settings."""
    _integer("position", self.position, 1, 5)
    if type(self.exposures) is not tuple or len(self.exposures) != 3:
      raise ValueError("Three immutable exposure times are required")
    times = tuple(CameraExposure(value).milliseconds for value in self.exposures)
    if len(set(times)) != 3:
      raise ValueError("Collection exposures must encode distinct millisecond values")


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
class CollectionPlan:
  """Explicit optical reads during selected holds, while thermal timing stays on the instrument.

  ``steps`` contains zero-based (cycle-group, step) pairs, applied to every repetition. The
  scheduler starts when remaining hold seconds enter ``read_window`` (default 45 seconds).
  A delay of more than two reported seconds misses the window and fails closed. This budget
  must accommodate all filter moves, exposure transfers and status queries on the control host;
  it is not a hardware trigger or a guarantee of subsecond synchronization. No hold is extended.
  The lamp must already be enabled at native power 1; this plan never implicitly changes it.
  """

  steps: tuple[tuple[int, int], ...]
  filters: tuple[CollectionFilter, ...]
  read_window: int = 45

  def __post_init__(self) -> None:
    """Reject empty, duplicate and unordered settings before any instrument I/O."""
    _integer("read_window", self.read_window, 5, 5939)
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
    if sum(sum(value.exposures) for value in self.filters) >= self.read_window - 2:
      raise ValueError("Read window cannot fit even the requested exposures")

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
    return tuple(sorted(result))

  def to_dict(self) -> dict[str, Any]:
    """Serialize immutable settings for comparison after restart."""
    return {
      "steps": [list(pair) for pair in self.steps],
      "filters": [{"position": f.position, "exposures": list(f.exposures)} for f in self.filters],
      "read_window": self.read_window,
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, Any]) -> "CollectionPlan":
    """Restore a validated plan without selecting defaults for missing fields."""
    return cls(
      tuple((pair[0], pair[1]) for pair in value["steps"]),
      tuple(CollectionFilter(f["position"], tuple(f["exposures"])) for f in value["filters"]),
      value["read_window"],
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

  @property
  def collection_id(self) -> str:
    """Stable identifier shared by the checkpoint and its individual camera intents."""
    return self.point.collection_id(self.run_id)

  def __post_init__(self) -> None:
    """Require complete identity and phase evidence before accepting a completed checkpoint."""
    _integer("ordinal", self.ordinal, 1, 32760)
    if self.state not in ("uncertain", "complete"):
      raise ValueError("Invalid collection state")
    if not self.point.matches(self.before):
      raise ValueError("Collection must begin within its designated hold")
    if type(self.acquisition_ids) is not tuple or not self.acquisition_ids:
      raise ValueError("Collection requires immutable acquisition identities")
    if len(set(self.acquisition_ids)) != len(self.acquisition_ids):
      raise ValueError("Duplicate collection acquisition identity")
    if self.state == "complete" and (
      self.after is None
      or not self.point.matches(self.after)
      or self.before.run_start_record != self.after.run_start_record
      or self.error is not None
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
    )


@dataclass(frozen=True)
class FluorescenceCollection:
  """A collection checkpoint and all persisted complete or partial original camera frames."""

  record: CollectionRecord
  acquisitions: tuple[CameraAcquisition, ...]

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
