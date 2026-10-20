"""Typed interpretation of captured thermal status and identification replies."""

import struct
from dataclasses import dataclass
from typing import Literal, Optional

from .protocol import ABI7500ProtocolError

SequencePhase = Literal["inactive", "ramping", "holding", "paused", "modified", "unknown"]
PlateState = Literal["unloaded", "moving", "loaded", "unknown"]
_PLATE_STATES: dict[int, PlateState] = {0: "unloaded", 1: "moving", 2: "loaded"}
_PHASES: dict[int, SequencePhase] = {
  0: "inactive",
  1: "ramping",
  2: "holding",
  4: "paused",
  8: "modified",
}


def _terminated_ascii(data: bytes) -> str:
  """Read a bounded C string without interpreting the bytes beyond its terminator."""
  if b"\x00" not in data:
    raise ABI7500ProtocolError("Identification string has no NUL terminator")
  try:
    return data.split(b"\x00", 1)[0].decode("ascii")
  except UnicodeDecodeError as exc:
    raise ABI7500ProtocolError("Identification string is not ASCII") from exc


@dataclass(frozen=True)
class ABI7500CoverState:
  """Native 0x36 readback, recovered from TC9700Control::GetCoverTemp.

  Setpoint is integer degrees Celsius. Mode 1 is the captured calibration control mode;
  mode 2 is the native alternative, whose behavior has not been validated. The final word
  remains raw because its units have not been confirmed by a capture. Use get_status()
  for the independently verified measured cover temperature in degrees Celsius.
  """

  raw_payload: bytes

  def __post_init__(self) -> None:
    """Require exactly three immutable big-endian words."""
    if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) != 6:
      raise ABI7500ProtocolError("Cover state must contain six bytes")

  @property
  def setpoint(self) -> int:
    """Integer-degree target encoded by SetCoverTemp."""
    return int.from_bytes(self.raw_payload[:2], "big")

  @property
  def mode(self) -> int:
    """Native control word, preserved without inventing meanings for unknown values."""
    return int.from_bytes(self.raw_payload[2:4], "big")

  @property
  def measured_raw(self) -> int:
    """Unscaled measurement word; its units remain hardware-unverified."""
    return int.from_bytes(self.raw_payload[4:], "big")


@dataclass(frozen=True)
class ABI7500PlateStatus:
  """Internal plate mechanism state from command 0x73, preserving the original reply.

  ``unloaded`` means the internal mechanism has released the plate. It does not establish that
  the external drawer is open, that a plate is present, or that the heated cover has cooled.
  """

  raw_payload: bytes

  def __post_init__(self) -> None:
    """Require the complete immutable two-byte block-state response."""
    if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) != 2:
      raise ABI7500ProtocolError("Plate status must contain two bytes")

  @property
  def state_code(self) -> int:
    """Return the big-endian native code, including unrecognized values."""
    return int.from_bytes(self.raw_payload, "big")

  @property
  def state(self) -> PlateState:
    """Interpret captured states without treating an unknown code as stationary."""
    return _PLATE_STATES.get(self.state_code, "unknown")


@dataclass(frozen=True)
class ABI7500Identity:
  """Camera identification and thermal firmware version, preserving both original replies.

  The camera identifier is not the thermal-controller or calibration serial number. Uninterpreted
  bytes following the strings can contain stale memory and are not additional identity fields.
  """

  camera_response: bytes
  thermal_response: bytes

  def __post_init__(self) -> None:
    """Validate the response lengths and bounded strings."""
    if not isinstance(self.camera_response, bytes) or len(self.camera_response) != 64:
      raise ABI7500ProtocolError("Camera identity must contain 64 bytes")
    if not isinstance(self.thermal_response, bytes) or len(self.thermal_response) != 70:
      raise ABI7500ProtocolError("Thermal identity must contain 70 bytes")
    _terminated_ascii(self.camera_response)
    _terminated_ascii(self.thermal_response[:56])

  @property
  def camera_description(self) -> str:
    """Return the camera's comma-separated identification string."""
    return _terminated_ascii(self.camera_response)

  @property
  def thermal_version(self) -> str:
    """Return the first thermal identification string, as the vendor GetVersionText does."""
    return _terminated_ascii(self.thermal_response[:56])


@dataclass(frozen=True)
class ABI7500Status:
  """One extended-state (0x29) response, retaining unclassified flags and timestamp bytes.

  Temperatures are degrees Celsius, times are seconds. Cycle and step indices are zero based
  firmware indices, not necessarily the experiment's stage/step indices. Repetition is one based
  during the captured run. Indices and hold time may be stale while idle; elapsed time can keep
  advancing. An inactive sequence does not imply a cool cover. A stopped state alone cannot
  distinguish successful completion from cancellation or identify who started a run.
  """

  raw_payload: bytes

  def __post_init__(self) -> None:
    """Require a complete, immutable extended-state reply."""
    if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) != 50:
      raise ABI7500ProtocolError("Extended thermal status must contain 50 bytes")

  @property
  def state_code(self) -> int:
    """Return the firmware state byte without assuming a complete state enumeration."""
    return self.raw_payload[1]

  @property
  def sequence_state_code(self) -> int:
    """Return the native sequence state byte."""
    return self.raw_payload[30]

  @property
  def phase(self) -> SequencePhase:
    """Interpret sequence states recognized by the native status routine.

    ``inactive`` means no active sequence step, not proof of a successfully completed experiment.
    """
    return _PHASES.get(self.sequence_state_code, "unknown")

  @property
  def run_start_record(self) -> bytes:
    """Return the opaque native start date record; it can describe the previous run."""
    return self.raw_payload[32:41]

  @property
  def block_temperature(self) -> float:
    """Return measured block temperature."""
    return float(struct.unpack_from(">I", self.raw_payload, 4)[0]) / 10.0

  @property
  def cover_temperature(self) -> float:
    """Return measured heated-cover temperature."""
    return float(struct.unpack_from(">I", self.raw_payload, 8)[0]) / 10.0

  @property
  def ambient_temperature(self) -> float:
    """Return measured ambient temperature."""
    return float(struct.unpack_from(">I", self.raw_payload, 12)[0]) / 10.0

  @property
  def sample_temperature(self) -> float:
    """Return the controller's sample-temperature estimate."""
    return float(struct.unpack_from(">H", self.raw_payload, 26)[0]) / 10.0

  @property
  def elapsed_time(self) -> float:
    """Return the elapsed counter, which can continue advancing with no active sequence."""
    return float(struct.unpack_from(">I", self.raw_payload, 16)[0])

  @property
  def remaining_hold_time(self) -> Optional[float]:
    """Return hold seconds (possibly stale when inactive), or None for the 0xffffffff sentinel."""
    value = struct.unpack_from(">I", self.raw_payload, 20)[0]
    return None if value == 0xFFFFFFFF else float(value)

  @property
  def cycle_index(self) -> int:
    """Return the native zero-based cycle-group index."""
    return self.raw_payload[28]

  @property
  def step_index(self) -> int:
    """Return the native zero-based step index within the group."""
    return self.raw_payload[29]

  @property
  def repetition(self) -> int:
    """Return the native repetition counter; only meaningful during an active sequence."""
    return self.raw_payload[31]
