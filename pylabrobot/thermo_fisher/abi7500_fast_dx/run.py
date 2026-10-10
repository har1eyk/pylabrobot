"""Thermal history and persistent run checkpoints, without instrument I/O."""

import re
import struct
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from .collection import CollectionPlan
from .protocol import ABI7500ProtocolError
from .status import ABI7500Status, _terminated_ascii
from .thermal_program import ThermalProgram

HistoryOutcome = Literal["completed", "aborted", "no_result", "unknown"]
PlateReleaseState = Literal["not_requested", "uncertain", "released"]
RunState = Literal[
  "preparing",
  "starting",
  "running",
  "cancelling",
  "uncertain",
  "completed",
  "cancelled",
  "aborted",
  "not_started",
]
_RUN_STATES = {
  "preparing",
  "starting",
  "running",
  "cancelling",
  "uncertain",
  "completed",
  "cancelled",
  "aborted",
  "not_started",
}
_TERMINAL_STATES = {"completed", "cancelled", "aborted", "not_started"}
_HISTORY_OUTCOMES: dict[int, HistoryOutcome] = {0: "completed", 1: "aborted", 253: "no_result"}


def _validate_run_id(run_id: str) -> None:
  """Require a native method label that can serve as an explicit run identity."""
  if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,16}", run_id) is None:
    raise ValueError("run_id must contain 1–16 ASCII letters, digits, underscores or hyphens")


def _validate_run_program(program: ThermalProgram) -> None:
  """Limit actuation to the reference Standard-mode, 20 µL, block-2 configuration."""
  if not isinstance(program, ThermalProgram):
    raise ValueError("program must be a ThermalProgram")
  if program.metadata[42:] != bytes.fromhex("1400020000"):
    raise ValueError(
      "Run execution supports only the captured Standard/20 µL/block-2 configuration"
    )
  for date in (program.metadata[24:33], program.metadata[33:42]):
    if not (
      date[0] <= 99
      and 1 <= date[1] <= 12
      and 1 <= date[2] <= 31
      and date[3] <= 2
      and date[4] <= 23
      and date[5] <= 59
      and date[6] <= 59
    ):
      raise ValueError("Invalid native program date record")


class ABI7500RunError(RuntimeError):
  """Run ownership or state cannot be established sufficiently to perform an operation."""


@dataclass(frozen=True)
class ABI7500History:
  """The 94-byte thermal history header returned by command 0x19.

  History describes the last recorded run, which can differ from the active sequence. Date records
  remain opaque nine-byte values: their clock need not agree with the host. Outcome alone is not
  proof that a particular PLR run completed. The full reply, including unused tails, is retained.
  """

  raw_payload: bytes

  def __post_init__(self) -> None:
    """Require the complete immutable native history header."""
    if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) != 94:
      raise ABI7500ProtocolError("Thermal history header must contain 94 bytes")

  @property
  def status_code(self) -> int:
    """Return the native run outcome code, independently of current instrument status."""
    return int(struct.unpack_from(">H", self.raw_payload, 6)[0])

  @property
  def outcome(self) -> HistoryOutcome:
    """Return the history result, without inferring current sequence activity.

    Code 253 means no final result: it also occurs with the current run's labels and start record
    during active ramping. It does not establish that no run has started.
    """
    return _HISTORY_OUTCOMES.get(self.status_code, "unknown")

  @property
  def run_start_record(self) -> bytes:
    """Return the native start date record for correlation, without host-clock conversion."""
    return self.raw_payload[10:19]

  @property
  def run_end_record(self) -> bytes:
    """Return the native end date record; only meaningful for a finished, matched run."""
    return self.raw_payload[19:28]

  @property
  def duration(self) -> float:
    """Return the recorded run duration in seconds."""
    return float(struct.unpack_from(">I", self.raw_payload, 28)[0])

  @property
  def reaction_volume(self) -> float:
    """Return the recorded reaction volume in microliters."""
    return float(struct.unpack_from(">H", self.raw_payload, 32)[0])

  @property
  def method_name(self) -> str:
    """Read the bounded method label, excluding stale bytes beyond the first NUL."""
    return _terminated_ascii(self.raw_payload[46:63])

  @property
  def user_name(self) -> str:
    """Read the bounded method user label."""
    return _terminated_ascii(self.raw_payload[63:70])


@dataclass(frozen=True)
class ThermalRun:
  """A journaled thermal run, optional optical plan, and most recent evidence.

  Reuse the same run ID and journal when retrying or reconnecting. IDs are one to sixteen ASCII
  letters, digits, underscores or hyphens; they are written into the instrument's method name.
  The user field is set to ``PLR``. Timing remains on the thermal controller. Optical results are
  separate collection records; ``completed`` denotes thermal completion, not optical success.
  """

  run_id: str
  program: ThermalProgram
  camera_description: str
  thermal_version: str
  baseline_start_record: bytes
  state: RunState = "preparing"
  start_acknowledged: bool = False
  start_record: Optional[bytes] = None
  last_status: Optional[ABI7500Status] = None
  history: Optional[ABI7500History] = None
  cancel_requested: bool = False
  idle_temperature_set: bool = False
  collection_plan: Optional[CollectionPlan] = None
  collection_error: Optional[str] = None
  plate_release_state: PlateReleaseState = "not_requested"

  def __post_init__(self) -> None:
    """Reject malformed checkpoints before they can authorize instrument writes."""
    _validate_run_id(self.run_id)
    if not isinstance(self.program, ThermalProgram):
      raise ValueError("program must be a ThermalProgram")
    if not isinstance(self.state, str) or self.state not in _RUN_STATES:
      raise ValueError("Invalid thermal run state")
    for label in (self.camera_description, self.thermal_version):
      if not isinstance(label, str) or not label:
        raise ValueError("Run checkpoints require camera and thermal identities")
    for record in (self.baseline_start_record, self.start_record):
      if record is not None and (not isinstance(record, bytes) or len(record) != 9):
        raise ValueError("Run date records must contain nine bytes")
    if self.baseline_start_record is None:
      raise ValueError("A baseline start record is required")
    for flag in (self.start_acknowledged, self.cancel_requested, self.idle_temperature_set):
      if not isinstance(flag, bool):
        raise ValueError("Run checkpoint flags must be booleans")
    if self.collection_plan is not None:
      self.collection_plan.points(self.program)
    if self.collection_error is not None and not isinstance(self.collection_error, str):
      raise ValueError("Collection error must be text")
    if self.plate_release_state not in ("not_requested", "uncertain", "released"):
      raise ValueError("Invalid plate release state")

  @property
  def wire_program(self) -> ThermalProgram:
    """Tag the uploaded method with this run ID, retaining dates and thermal configuration."""
    metadata = (
      self.run_id.encode("ascii").ljust(17, b"\x00")
      + b"PLR\x00\x00\x00\x00"
      + self.program.metadata[24:]
    )
    return ThermalProgram(metadata, self.program.cycles)

  @property
  def is_terminal(self) -> bool:
    """Whether the journal has resolved the run's outcome."""
    return self.state in _TERMINAL_STATES

  def to_dict(self) -> dict[str, Any]:
    """Serialize all checkpoint evidence for the durable journal."""
    return {
      "run_id": self.run_id,
      "program_hex": self.program.encode().hex(),
      "camera_description": self.camera_description,
      "thermal_version": self.thermal_version,
      "baseline_start_record": self.baseline_start_record.hex(),
      "state": self.state,
      "start_acknowledged": self.start_acknowledged,
      "start_record": None if self.start_record is None else self.start_record.hex(),
      "status_hex": None if self.last_status is None else self.last_status.raw_payload.hex(),
      "history_hex": None if self.history is None else self.history.raw_payload.hex(),
      "cancel_requested": self.cancel_requested,
      "idle_temperature_set": self.idle_temperature_set,
      "collection_plan": None if self.collection_plan is None else self.collection_plan.to_dict(),
      "collection_error": self.collection_error,
      "plate_release_state": self.plate_release_state,
    }

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> "ThermalRun":
    """Restore a checkpoint with the same payload and field validation as a fresh object."""
    return cls(
      run_id=data["run_id"],
      program=ThermalProgram.decode(bytes.fromhex(data["program_hex"])),
      camera_description=data["camera_description"],
      thermal_version=data["thermal_version"],
      baseline_start_record=bytes.fromhex(data["baseline_start_record"]),
      state=data["state"],
      start_acknowledged=data["start_acknowledged"],
      start_record=None if data["start_record"] is None else bytes.fromhex(data["start_record"]),
      last_status=None
      if data["status_hex"] is None
      else ABI7500Status(bytes.fromhex(data["status_hex"])),
      history=None
      if data["history_hex"] is None
      else ABI7500History(bytes.fromhex(data["history_hex"])),
      cancel_requested=data["cancel_requested"],
      idle_temperature_set=data["idle_temperature_set"],
      collection_plan=None
      if data.get("collection_plan") is None
      else CollectionPlan.from_dict(data["collection_plan"]),
      collection_error=data.get("collection_error"),
      plate_release_state=data.get("plate_release_state", "not_requested"),
    )
