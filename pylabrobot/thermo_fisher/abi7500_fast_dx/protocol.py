"""Pure codecs for the 7500 software's thermal packets and Hamamatsu serial tunnel.

These functions operate on bytes only. They do not initialize a camera, communicate with hardware,
interpret thermal state codes, or establish compatibility with Fast Dx firmware.
"""

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

_MAX_SERIAL_SIZE = 1024
_THERMAL_OVERHEAD = 21
_THERMAL_HEADER = struct.Struct(">2s4sH4sHHI")


class ABI7500ProtocolError(ValueError):
  """Bytes do not conform to the recovered packet format."""


def _check_unsigned(name: str, value: int, maximum: int) -> None:
  """Reject values that cannot be represented in the specified wire field."""
  if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
    raise ValueError(f"{name} must be an integer between 0 and {maximum}")


def _thermal_size(prefix: bytes) -> int:
  """Validate an eight-byte prefix and return the complete packet size."""
  if len(prefix) < 8:
    raise ABI7500ProtocolError("Truncated thermal header")
  if prefix[:2] != b"XX":
    raise ABI7500ProtocolError("Invalid thermal packet marker")
  size = int.from_bytes(prefix[6:8], "big") + 6
  if not _THERMAL_OVERHEAD <= size <= _MAX_SERIAL_SIZE:
    raise ABI7500ProtocolError(f"Invalid thermal packet size: {size}")
  return size


@dataclass(frozen=True)
class ThermalPacket:
  """A thermal-controller packet, without the outer Hamamatsu tunnel header.

  Args:
    command: Unsigned 16-bit command identifier. This codec does not validate command semantics.
    sequence: Unsigned 32-bit message index. Allocation and response matching belong to the caller.
    payload: Opaque binary payload, at most 1003 bytes.
    error_code: Unsigned 16-bit error field. Requests use zero; error meanings remain unverified.
    address: Four opaque address bytes. The reference request encoder uses all ones.
  """

  command: int
  sequence: int
  payload: bytes = b""
  error_code: int = 0
  address: bytes = b"\xff\xff\xff\xff"

  def __post_init__(self) -> None:
    """Validate field widths and preserve immutable byte payloads."""
    _check_unsigned("command", self.command, 0xFFFF)
    _check_unsigned("sequence", self.sequence, 0xFFFFFFFF)
    _check_unsigned("error_code", self.error_code, 0xFFFF)
    if not isinstance(self.address, bytes) or len(self.address) != 4:
      raise ValueError("address must be exactly four bytes")
    if not isinstance(self.payload, bytes):
      raise ValueError("payload must be bytes")
    if len(self.payload) > _MAX_SERIAL_SIZE - _THERMAL_OVERHEAD:
      raise ValueError("Thermal payload exceeds 1003 bytes")

  def encode(self) -> bytes:
    """Encode the packet, including its length and additive checksum.

    The checksum covers the length field through the payload. There is no trailing delimiter.
    """
    packet = (
      _THERMAL_HEADER.pack(
        b"XX",
        self.address,
        len(self.payload) + 15,
        b"9700",
        self.command,
        self.error_code,
        self.sequence,
      )
      + self.payload
    )
    return packet + bytes([sum(packet[6:]) & 0xFF])

  @classmethod
  def decode(cls, data: bytes) -> "ThermalPacket":
    """Decode exactly one complete packet, rejecting corruption and trailing data.

    Nonzero error fields are preserved without assigning unverified device error meanings.

    Raises:
      ABI7500ProtocolError: The marker, size, signature, or checksum is invalid.
    """
    size = _thermal_size(data)
    if len(data) != size:
      raise ABI7500ProtocolError(f"Expected {size} thermal bytes, received {len(data)}")
    if data[8:12] != b"9700":
      raise ABI7500ProtocolError("Invalid thermal protocol signature")
    if (sum(data[6:-1]) & 0xFF) != data[-1]:
      raise ABI7500ProtocolError("Thermal checksum mismatch")
    _, address, _, _, command, error_code, sequence = _THERMAL_HEADER.unpack_from(data)
    return cls(command, sequence, data[20:-1], error_code, address)


class ThermalPacketDecoder:
  """Assemble thermal packets from fragmented or coalesced tunnel payloads.

  This is a strict offline stream decoder, not a response matcher. After malformed input or a
  truncated stream, call :meth:`reset` before using it for a separate stream. It never scans past
  corrupt bytes, since a packet marker can also occur inside a binary payload. If a feed fails,
  none of that call's packets are returned.
  """

  def __init__(self) -> None:
    """Create an empty stream decoder."""
    self._buffer = bytearray()
    self._failed = False

  def reset(self) -> None:
    """Discard buffered bytes and errors before starting a separate stream."""
    self._buffer.clear()
    self._failed = False

  def _check_active(self) -> None:
    """Reject continuation of a stream that already failed validation."""
    if self._failed:
      raise ABI7500ProtocolError("Decoder failed; reset before starting a separate stream")

  def feed(self, data: bytes) -> List[ThermalPacket]:
    """Return complete packets and retain an incomplete final packet for the next call."""
    self._check_active()
    self._buffer.extend(data)
    packets: List[ThermalPacket] = []
    try:
      while len(self._buffer) >= 8:
        size = _thermal_size(bytes(self._buffer[:8]))
        if len(self._buffer) < size:
          break
        packets.append(ThermalPacket.decode(bytes(self._buffer[:size])))
        del self._buffer[:size]
    except ABI7500ProtocolError:
      self._buffer.clear()
      self._failed = True
      raise
    return packets

  def finish(self) -> None:
    """Check that the stream ended at a packet boundary; reject a truncated final packet."""
    self._check_active()
    if self._buffer:
      remaining = len(self._buffer)
      self._buffer.clear()
      self._failed = True
      raise ABI7500ProtocolError(f"Truncated thermal stream: {remaining} unconsumed bytes")


def encode_serial_write(payload: bytes) -> Tuple[bytes, ...]:
  """Wrap serial bytes in an RS command and split into at most 64-byte writes.

  The length is four space-padded ASCII decimal digits. No terminator or zero-length packet is
  appended. The 1024-byte serial limit is distinct from the six-byte wrapper.
  """
  if len(payload) > _MAX_SERIAL_SIZE:
    raise ValueError("Serial payload exceeds 1024 bytes")
  wrapped = b"RS" + f"{len(payload):4d}".encode("ascii") + payload
  return tuple(wrapped[offset : offset + 64] for offset in range(0, len(wrapped), 64))


def encode_serial_read(size: int) -> bytes:
  """Encode an RR request for up to ``size`` serial bytes (0 to 1024).

  The requested wire length includes the two-byte count prefix in the reply.
  """
  _check_unsigned("size", size, _MAX_SERIAL_SIZE)
  return b"RR" + f"{size + 2:4d}".encode("ascii")


def decode_serial_read(data: bytes, *, requested_size: Optional[int] = None) -> bytes:
  """Extract valid serial bytes from one fully assembled RR reply.

  The first two bytes are a little-endian valid-byte count. A device may return fewer valid bytes
  than requested while still filling the requested USB reply length with arbitrary trailing bytes.
  Only counted bytes belong to the thermal stream; even a zero count can have nonzero padding.

  Args:
    data: Complete USB reply, including the count and any padding.
    requested_size: Serial capacity passed to :func:`encode_serial_read`, excluding the count.
      When supplied, require exactly ``requested_size + 2`` wire bytes and discard only the
      uncounted tail. When omitted, accept only an unpadded reply.

  Raises:
    ValueError: The requested size is outside the serial capacity range.
    ABI7500ProtocolError: The count or complete reply length is invalid.
  """
  if requested_size is not None:
    _check_unsigned("requested_size", requested_size, _MAX_SERIAL_SIZE)
  if len(data) < 2:
    raise ABI7500ProtocolError("Truncated serial count")
  size = int.from_bytes(data[:2], "little")
  if size > _MAX_SERIAL_SIZE:
    raise ABI7500ProtocolError(f"Invalid serial payload size: {size}")
  capacity = size if requested_size is None else requested_size
  if size > capacity:
    raise ABI7500ProtocolError(f"Serial count {size} exceeds requested capacity {capacity}")
  if len(data) != capacity + 2:
    raise ABI7500ProtocolError(f"Expected {capacity + 2} serial reply bytes, received {len(data)}")
  return data[2 : size + 2]
