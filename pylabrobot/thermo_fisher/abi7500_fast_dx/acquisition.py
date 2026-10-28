"""Raw, journaled camera snapshots without well mapping or fluorescence correction."""

import math
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from .camera import CameraFrame, CameraGeometry
from .protocol import ABI7500ProtocolError
from .status import ABI7500Status


def _validate_acquisition_id(acquisition_id: str) -> None:
  """Require an explicit local identifier for a single exposure intent."""
  if (
    not isinstance(acquisition_id, str)
    or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", acquisition_id) is None
  ):
    raise ValueError(
      "acquisition_id must contain 1–64 ASCII letters, digits, underscores or hyphens"
    )


@dataclass(frozen=True)
class CameraExposure:
  """A requested exposure in seconds for the recovered C9254-03/2.20 and C9254-02/2.19 CJ command.

  The experimental snapshot API limits requests to 0.005–2 seconds: the native minimum and
  the longest exposure in the optical-calibration capture. This is not a measured effective
  integration time. The setter rounds with a 0.49 ms bias and truncation, rather than Python round.
  """

  duration: float

  def __post_init__(self) -> None:
    """Reject unsupported or nonfinite exposure requests before any instrument I/O."""
    if isinstance(self.duration, bool) or not math.isfinite(self.duration):
      raise ValueError("Exposure must be finite")
    if not 0.005 <= self.duration <= 2:
      raise ValueError("Experimental snapshots support exposures from 0.005 to 2 seconds")

  @property
  def milliseconds(self) -> int:
    """Return the integer sent to firmware, using the reference setter's conversion."""
    return int((self.duration + 0.00049) * 1000)

  def encode(self) -> bytes:
    """Encode CJ with its two-byte response length and five-character millisecond field."""
    return f"CJ{2:4d},{self.milliseconds:5d}".encode("ascii")

  def validate_reply(self, reply: bytes) -> None:
    """Require the captured little-endian millisecond echo before starting an exposure."""
    if reply != self.milliseconds.to_bytes(2, "little"):
      raise ABI7500ProtocolError("Camera exposure reply does not match the requested milliseconds")


@dataclass(frozen=True)
class StoredExposureTimes:
  """The reference controller's 254-byte exposure table, preserving all original bytes.

  Five physical filters each have three signed big-endian millisecond values, starting at
  byte two. The leading two bytes and trailing 192 bytes remain uninterpreted. This readback
  does not establish calibration validity or guarantee that a reported duration is supported
  by :class:`CameraExposure`. Reading it does not change a PLR collection plan.
  """

  raw_payload: bytes

  def __post_init__(self) -> None:
    """Require the immutable, complete reference table before interpreting its fields."""
    if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) != 254:
      raise ABI7500ProtocolError("Expected exactly 254 immutable exposure-table bytes")

  def for_filter(self, filter: int) -> tuple[float, float, float]:
    """Return reported durations in seconds, preserving the instrument's exposure order.

    Args:
      filter: Physical wheel position 1–5, not the reversed saved/vendor channel number.
        Physical position 5 corresponds to saved channel 1 on the reference instrument.

    Zero and negative values are retained without treating them as supported exposure requests.
    """
    if type(filter) is not int or not 1 <= filter <= 5:
      raise ValueError("Physical filter must be an integer from 1 to 5")
    start = 2 + (filter - 1) * 12
    return (
      int.from_bytes(self.raw_payload[start : start + 4], "big", signed=True) / 1000,
      int.from_bytes(self.raw_payload[start + 4 : start + 8], "big", signed=True) / 1000,
      int.from_bytes(self.raw_payload[start + 8 : start + 12], "big", signed=True) / 1000,
    )


@dataclass(frozen=True)
class OpticalState:
  """Uncorrected optical observations, retaining all native reply bytes.

  Filter positions and lamp power are native indices, not dye names or measured intensity.
  Equality before and after a snapshot does not establish optical calibration validity.
  """

  filter_response: bytes
  motor_response: bytes
  lamp_response: bytes

  def __post_init__(self) -> None:
    """Require exactly the captured fixed-size replies."""
    for data, size in (
      (self.filter_response, 2),
      (self.motor_response, 2),
      (self.lamp_response, 4),
    ):
      if not isinstance(data, bytes) or len(data) != size:
        raise ABI7500ProtocolError("Invalid optical status reply length")

  @property
  def filter_position(self) -> int:
    """Return the big-endian native filter index from command 0x6b."""
    return int.from_bytes(self.filter_response, "big")

  @property
  def is_stationary(self) -> bool:
    """Whether command 0x6c returned the captured stationary motor state."""
    return self.motor_response == b"\x00\x00"

  @property
  def lamp_enabled(self) -> bool:
    """Decode the enable word; reject unknown states instead of treating them as off."""
    value = int.from_bytes(self.lamp_response[:2], "big")
    if value not in (0, 1):
      raise ABI7500ProtocolError(f"Unknown lamp enable state: {value}")
    return value == 1

  @property
  def lamp_power(self) -> int:
    """Return the native power word: settings 0/1, or observed off-state sentinel 2.

    These codes do not specify calibrated light output or prove that the lamp is emitting light.
    """
    return int.from_bytes(self.lamp_response[2:], "big")

  def to_dict(self) -> dict[str, str]:
    """Serialize original reply bytes without losing unknown state codes."""
    return {
      "filter": self.filter_response.hex(),
      "motor": self.motor_response.hex(),
      "lamp": self.lamp_response.hex(),
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, str]) -> "OpticalState":
    """Restore native optical replies from a journal record."""
    return cls(*(bytes.fromhex(value[key]) for key in ("filter", "motor", "lamp")))


@dataclass(frozen=True)
class CameraAcquisition:
  """One exposure intent and its immutable original data and host observations.

  ``uncertain`` means the operation was interrupted or its final observations disagreed. It
  does not prove that CC was sent or that no exposure occurred. Reusing an ID never repeats CC.
  A new ID explicitly requests another exposure; it does not recover an incomplete old frame.
  Host timestamps alone do not establish synchronization with a thermal cycle. Scheduled reads
  also require a CollectionRecord with the intended hold and matching controller observations;
  this establishes hold association, not a hardware trigger or subsecond timing precision.
  ``observation_scope="filter_burst"`` means all frames in that filter share the observations
  before and after the burst. No intermediate state stability is claimed. ``exposure_start_offset``
  records the host's CC submission time relative to the collection's first CC, not a camera trigger.
  """

  acquisition_id: str
  exposure: CameraExposure
  camera_description: str
  thermal_version: str
  geometry: CameraGeometry
  started_at: str
  thermal_before: ABI7500Status
  optics_before: OpticalState
  state: Literal["uncertain", "complete"] = "uncertain"
  finished_at: Optional[str] = None
  exposure_reply: bytes = b""
  readiness_replies: tuple[bytes, ...] = ()
  wire_data: bytes = b""
  thermal_after: Optional[ABI7500Status] = None
  optics_after: Optional[OpticalState] = None
  error: Optional[str] = None
  observation_scope: Literal["frame", "filter_burst"] = "frame"
  exposure_start_offset: Optional[float] = None

  def __post_init__(self) -> None:
    """Reject corrupt identities, incomplete successful results and oversized data."""
    _validate_acquisition_id(self.acquisition_id)
    if self.observation_scope not in ("frame", "filter_burst"):
      raise ValueError("Invalid acquisition observation scope")
    if self.exposure_start_offset is not None and (
      self.observation_scope != "filter_burst"
      or isinstance(self.exposure_start_offset, bool)
      or not math.isfinite(self.exposure_start_offset)
      or self.exposure_start_offset < 0
    ):
      raise ValueError("Exposure start offset requires finite nonnegative burst timing")
    if self.state not in ("uncertain", "complete"):
      raise ValueError("Invalid camera acquisition state")
    if not isinstance(self.wire_data, bytes) or len(self.wire_data) > self.geometry.transfer_size:
      raise ValueError("Invalid camera acquisition data")
    if self.state == "complete":
      self.exposure.validate_reply(self.exposure_reply)
      if (
        len(self.wire_data) != self.geometry.transfer_size
        or self.thermal_after is None
        or self.optics_after is None
        or self.finished_at is None
        or not self.readiness_replies
        or self.readiness_replies[-1] != b"ready"
        or self.error is not None
        or (self.observation_scope == "filter_burst" and self.exposure_start_offset is None)
      ):
        raise ValueError("Complete acquisition requires a full frame and final observations")

  @property
  def frame(self) -> Optional[CameraFrame]:
    """Return a complete original frame when available, even if final status was uncertain."""
    if len(self.wire_data) == self.geometry.transfer_size:
      return CameraFrame(self.geometry, self.wire_data)
    return None

  def to_dict(self) -> dict[str, Any]:
    """Serialize a lossless checkpoint, including partial transfers and opaque status bytes."""
    return {
      "acquisition_id": self.acquisition_id,
      "exposure": self.exposure.duration,
      "camera_description": self.camera_description,
      "thermal_version": self.thermal_version,
      "geometry": [self.geometry.wire_width, self.geometry.wire_height],
      "started_at": self.started_at,
      "thermal_before": self.thermal_before.raw_payload.hex(),
      "optics_before": self.optics_before.to_dict(),
      "state": self.state,
      "finished_at": self.finished_at,
      "exposure_reply": self.exposure_reply.hex(),
      "readiness_replies": [reply.hex() for reply in self.readiness_replies],
      "wire_data": self.wire_data.hex(),
      "thermal_after": None if self.thermal_after is None else self.thermal_after.raw_payload.hex(),
      "optics_after": None if self.optics_after is None else self.optics_after.to_dict(),
      "error": self.error,
      **(
        {
          "observation_scope": self.observation_scope,
          "exposure_start_offset": self.exposure_start_offset,
        }
        if self.observation_scope != "frame"
        else {}
      ),
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, Any]) -> "CameraAcquisition":
    """Restore one checkpoint without reading the instrument or filling missing data."""
    return cls(
      acquisition_id=value["acquisition_id"],
      exposure=CameraExposure(value["exposure"]),
      camera_description=value["camera_description"],
      thermal_version=value["thermal_version"],
      geometry=CameraGeometry(*value["geometry"]),
      started_at=value["started_at"],
      thermal_before=ABI7500Status(bytes.fromhex(value["thermal_before"])),
      optics_before=OpticalState.from_dict(value["optics_before"]),
      state=value["state"],
      finished_at=value["finished_at"],
      exposure_reply=bytes.fromhex(value["exposure_reply"]),
      readiness_replies=tuple(bytes.fromhex(reply) for reply in value["readiness_replies"]),
      wire_data=bytes.fromhex(value["wire_data"]),
      thermal_after=None
      if value["thermal_after"] is None
      else ABI7500Status(bytes.fromhex(value["thermal_after"])),
      optics_after=None
      if value["optics_after"] is None
      else OpticalState.from_dict(value["optics_after"]),
      error=value["error"],
      observation_scope=value.get("observation_scope", "frame"),
      exposure_start_offset=value.get("exposure_start_offset"),
    )
