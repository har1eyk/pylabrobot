"""Offline thermal-program records, validated against native encoding and USB capture.

These objects encode the thermal controller's cycle/hold records, not a complete qPCR experiment.
Optical collection settings and cover temperature are configured separately by the vendor. Encoding
a program does not prove that it is accepted by a particular block or firmware.
"""

import math
import struct
from dataclasses import dataclass
from typing import Tuple

from .protocol import ABI7500ProtocolError


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
  """Require an integer in the range supported by the native method validator."""
  if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
    raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
  return value


def _tenths(name: str, value: float, minimum: int, maximum: int) -> int:
  """Convert degrees to integer tenths without silently rounding a requested temperature."""
  if isinstance(value, bool) or not math.isfinite(value):
    raise ValueError(f"{name} must be finite")
  scaled = value * 10
  encoded = round(scaled)
  if not math.isclose(scaled, encoded, rel_tol=0, abs_tol=1e-9):
    raise ValueError(f"{name} must be a multiple of 0.1 degrees")
  return _integer(name, encoded, minimum, maximum)


@dataclass(frozen=True)
class ThermalStep:
  """A finite temperature hold, with optional per-repetition increments.

  Temperatures are Celsius and times are integer seconds. Ramp rate is a percentage; acceptance
  of a particular rate remains block/firmware dependent. Infinite holds, pause records and 9600
  emulation are deliberately unsupported. Optical acquisition is not encoded in this record.
  """

  temperature: float
  hold_time: int
  ramp_rate: int = 100
  hold_time_increment: int = 0
  temperature_increment: float = 0.0

  def __post_init__(self) -> None:
    """Validate representable values and native finite-hold limits."""
    _tenths("temperature", self.temperature, 40, 999)
    _integer("hold_time", self.hold_time, 0, 5939)
    _integer("ramp_rate", self.ramp_rate, 1, 100)
    _integer("hold_time_increment", self.hold_time_increment, -599, 599)
    _tenths("temperature_increment", self.temperature_increment, -99, 99)

  def encode(self) -> bytes:
    """Return one native ten-byte setpoint record."""
    return struct.pack(
      ">BBHHhbB",
      3,
      0,
      self.hold_time,
      _tenths("temperature", self.temperature, 40, 999),
      self.hold_time_increment,
      _tenths("temperature_increment", self.temperature_increment, -99, 99),
      self.ramp_rate,
    )


@dataclass(frozen=True)
class ThermalCycle:
  """A group of up to seven finite holds repeated one to 99 times."""

  repetitions: int
  steps: Tuple[ThermalStep, ...]

  def __post_init__(self) -> None:
    """Validate group limits and the effective temperatures/times across all repetitions."""
    _integer("repetitions", self.repetitions, 1, 99)
    if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= 7:
      raise ValueError("steps must be a tuple containing one to seven thermal steps")
    for step in self.steps:
      if not isinstance(step, ThermalStep):
        raise ValueError("Each cycle step must be a ThermalStep")
      _tenths(
        "final temperature",
        step.temperature + (self.repetitions - 1) * step.temperature_increment,
        40,
        999,
      )
      _integer(
        "final hold_time",
        step.hold_time + (self.repetitions - 1) * step.hold_time_increment,
        0,
        5939,
      )

  def encode(self) -> bytes:
    """Return a native cycle header and its setpoint records."""
    return (
      bytes([2, 0, self.repetitions, len(self.steps)])
      + bytes(6)
      + b"".join(step.encode() for step in self.steps)
    )


@dataclass(frozen=True)
class ThermalProgram:
  """A lossless subset of the 0x12 program-upload payload, for offline protocol work.

  ``metadata`` retains the 47 bytes after the cycle-count byte: name/user strings, native date
  records, volume, block type, run mode and tube type. These fields must come from a compatible
  reference program; their presence is not hardware compatibility validation. In particular, a
  saved experiment's acquisition flags are not part of this thermal payload.
  """

  metadata: bytes
  cycles: Tuple[ThermalCycle, ...]

  def __post_init__(self) -> None:
    """Validate the bounded header and native maximum of 49 cycle/step records."""
    if not isinstance(self.metadata, bytes) or len(self.metadata) != 47:
      raise ValueError("metadata must contain the 47 native program-header bytes")
    if self.metadata[16] or self.metadata[23]:
      raise ValueError("Method and user name fields must end with NUL")
    if not isinstance(self.cycles, tuple) or not 1 <= len(self.cycles) <= 48:
      raise ValueError("cycles must be a tuple of one to 48 thermal cycles")
    if any(not isinstance(cycle, ThermalCycle) for cycle in self.cycles):
      raise ValueError("Each cycle must be a ThermalCycle")
    if sum(1 + len(cycle.steps) for cycle in self.cycles) > 49:
      raise ValueError("Thermal program exceeds 49 cycle and setpoint records")

  def encode(self) -> bytes:
    """Encode the complete thermal payload without USB or thermal packet framing."""
    return (
      bytes([len(self.cycles)]) + self.metadata + b"".join(cycle.encode() for cycle in self.cycles)
    )

  @classmethod
  def decode(cls, payload: bytes) -> "ThermalProgram":
    """Decode cycle/setpoint records; reject unsupported modes, truncation and extra records."""
    if len(payload) < 48 or (len(payload) - 48) % 10:
      raise ABI7500ProtocolError("Invalid thermal program length")
    offset = 48
    cycles = []
    try:
      for _ in range(payload[0]):
        header = payload[offset : offset + 10]
        if len(header) != 10 or header[:2] != b"\x02\x00" or header[4:] != bytes(6):
          raise ABI7500ProtocolError("Invalid or unsupported thermal cycle record")
        offset += 10
        steps = []
        for _ in range(header[3]):
          record = payload[offset : offset + 10]
          if len(record) != 10 or record[:2] != b"\x03\x00":
            raise ABI7500ProtocolError("Invalid or unsupported thermal setpoint record")
          _, _, hold, temperature, hold_increment, temperature_increment, ramp = struct.unpack(
            ">BBHHhbB", record
          )
          steps.append(
            ThermalStep(temperature / 10, hold, ramp, hold_increment, temperature_increment / 10)
          )
          offset += 10
        cycles.append(ThermalCycle(header[2], tuple(steps)))
      if offset != len(payload):
        raise ABI7500ProtocolError("Unexpected records after the declared cycle groups")
      return cls(payload[1:48], tuple(cycles))
    except ValueError as exc:
      raise ABI7500ProtocolError(f"Invalid thermal program: {exc}") from exc
