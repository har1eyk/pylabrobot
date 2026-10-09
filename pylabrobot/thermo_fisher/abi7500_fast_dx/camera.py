"""Offline reconstruction of Hamamatsu C9254 camera transfers.

The output matches the reference ArpLib image copy. It does not assign physical wells, dyes,
exposure units, or cycles, and does not apply fluorescence calibration.
"""

import struct
from dataclasses import dataclass
from typing import Tuple

from .protocol import ABI7500ProtocolError, _check_unsigned


@dataclass(frozen=True)
class CameraGeometry:
  """Original dimensions from the four-byte CI reply, before the reference library's crop."""

  wire_width: int
  wire_height: int

  @property
  def width(self) -> int:
    """Number of output columns in either reference copy mode."""
    return self.wire_width - 1

  @property
  def height(self) -> int:
    """Number of output rows in the default reference copy mode."""
    return self.wire_height - 5

  @property
  def block_count(self) -> int:
    """Number of 64-byte blocks requested for a 16-bit image.

    This is the reference formula, including its extra blocks at exact multiples; it is not a
    simple rounding of byte length to the next USB packet.
    """
    return 2 * (self.wire_width * self.wire_height // 64 + 1)

  @property
  def transfer_size(self) -> int:
    """Total bytes requested by CD, including the uninterpreted tail."""
    return 64 * self.block_count

  @property
  def pixel_data_size(self) -> int:
    """Bytes occupied by the original wire dimensions, before cropping."""
    return 2 * self.wire_width * self.wire_height

  def __post_init__(self) -> None:
    """Require usable dimensions and a transfer count that fits the recovered CD format."""
    _check_unsigned("wire_width", self.wire_width, 0xFFFF)
    _check_unsigned("wire_height", self.wire_height, 0xFFFF)
    if self.wire_width < 2 or self.wire_height < 6:
      raise ValueError("Camera dimensions must include the reference crop margins")
    if self.block_count > 9999:
      raise ValueError("Camera transfer exceeds the recovered four-character block count")

  @classmethod
  def decode(cls, data: bytes) -> "CameraGeometry":
    """Decode exactly one CI reply with two little-endian unsigned 16-bit dimensions."""
    if len(data) != 4:
      raise ABI7500ProtocolError("Expected exactly four camera dimension bytes")
    width, height = struct.unpack("<HH", data)
    try:
      return cls(width, height)
    except ValueError as exc:
      raise ABI7500ProtocolError(f"Unsupported camera geometry: {width} by {height}") from exc

  def encode_read(self) -> bytes:
    """Encode the reference CD read request, including its final space."""
    return f"CD{64:4d},{self.block_count:4d} ".encode("ascii")


@dataclass(frozen=True)
class CameraFrame:
  """An immutable complete camera transfer with a lossless reference image view.

  Args:
    geometry: Dimensions obtained from CI for this transfer.
    wire_data: Original CD response, including margin pixels and the uninterpreted tail.
    include_optical_black: Select the reference library's OPB=1 copy mode. The default uses OPB=0.
      Neither mode establishes the physical A1 orientation or applies calibration.
  """

  geometry: CameraGeometry
  wire_data: bytes
  include_optical_black: bool = False

  def __post_init__(self) -> None:
    """Reject mutable or incomplete data and invalid copy modes."""
    if not isinstance(self.wire_data, bytes):
      raise ValueError("wire_data must be immutable bytes")
    if not isinstance(self.include_optical_black, bool):
      raise ValueError("include_optical_black must be a boolean")
    if len(self.wire_data) != self.geometry.transfer_size:
      raise ABI7500ProtocolError(
        f"Expected {self.geometry.transfer_size} camera bytes, received {len(self.wire_data)}"
      )

  @property
  def width(self) -> int:
    """Number of columns in the reference image view."""
    return self.geometry.width

  @property
  def height(self) -> int:
    """Number of rows in the selected reference copy mode."""
    return self.geometry.wire_height if self.include_optical_black else self.geometry.height

  @property
  def transfer_tail(self) -> bytes:
    """Uninterpreted bytes following the wire pixel array, preserved without zero assumptions."""
    return self.wire_data[self.geometry.pixel_data_size :]

  @property
  def image_bytes(self) -> bytes:
    """Return row-major little-endian uint16 pixels, matching ARPAPI_GetImageData.

    In default mode, copying begins one pixel before wire row 5. Each subsequent row advances by
    the full wire width. OPB=1 starts at row 0 without that one-pixel offset. Do not replace this
    with a rectangular slice starting at column zero of row 5.
    """
    first_row = 0 if self.include_optical_black else 5
    pixel_offset = 0 if self.include_optical_black else -1
    rows = []
    for row in range(first_row, self.geometry.wire_height):
      start = 2 * (row * self.geometry.wire_width + pixel_offset)
      rows.append(self.wire_data[start : start + 2 * self.width])
    return b"".join(rows)

  def pixel_rows(self) -> Tuple[Tuple[int, ...], ...]:
    """Return unsigned pixel values in reference image order, without rescaling or correction."""
    data = self.image_bytes
    row_struct = struct.Struct(f"<{self.width}H")
    return tuple(row_struct.unpack_from(data, row * row_struct.size) for row in range(self.height))


class CameraFrameAssembler:
  """Assemble exactly one CD response from arbitrary USB read fragments.

  Transfer lengths, rather than USB packet boundaries or pixel values, delimit a frame. Create a
  separate assembler for every CD request. An overflow or truncated finish permanently rejects
  this transfer; a later read must not silently fill it with bytes from another operation.
  """

  def __init__(self, geometry: CameraGeometry, *, include_optical_black: bool = False) -> None:
    """Create an empty assembler for one geometry and reference copy mode."""
    if not isinstance(include_optical_black, bool):
      raise ValueError("include_optical_black must be a boolean")
    self._geometry = geometry
    self._include_optical_black = include_optical_black
    self._buffer = bytearray()
    self._failed = False
    self._finished = False

  @property
  def remaining(self) -> int:
    """Number of bytes still needed for this response."""
    return self._geometry.transfer_size - len(self._buffer)

  def _check_active(self) -> None:
    """Reject operations after a failed or finished transfer."""
    if self._failed or self._finished:
      raise ABI7500ProtocolError("Camera transfer is closed; create a new assembler")

  def feed(self, data: bytes) -> None:
    """Append a fragment, rejecting bytes beyond the exact requested transfer size."""
    self._check_active()
    if len(data) > self.remaining:
      self._failed = True
      raise ABI7500ProtocolError("Camera response exceeds the requested transfer size")
    self._buffer.extend(data)

  def finish(self) -> CameraFrame:
    """Return the complete frame, or reject an incomplete response."""
    self._check_active()
    if self.remaining:
      self._failed = True
      raise ABI7500ProtocolError(f"Truncated camera response: missing {self.remaining} bytes")
    self._finished = True
    return CameraFrame(self._geometry, bytes(self._buffer), self._include_optical_black)
