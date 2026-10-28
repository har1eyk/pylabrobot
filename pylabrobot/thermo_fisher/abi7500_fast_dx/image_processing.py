"""Offline camera corrections and diagnostic measurements using explicit pixel masks.

These are image-processing stages, not the vendor's complete fluorescence pipeline. Mask order
does not independently verify physical plate orientation or bind a calibration to an instrument.
"""

import hashlib
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional, Tuple

from .acquisition import CameraAcquisition
from .camera import CameraFrame, CameraGeometry
from .protocol import ABI7500ProtocolError, _check_unsigned

_PLR_MASK_HEADER = b"PLR-ABI7500-ROI\x01"


@dataclass(frozen=True)
class OpticalBlackImage:
  """A separate image corrected by the recovered HAMCamera optical-black algorithm.

  The original transfer remains in ``source``. This correction uses the OPB=1 copy, removes its
  first five rows, subtracts the truncated mean of its first two rows, and adds 128. Lower values
  clip to zero; native uint16 storage wraps overflow. This is not fluorescence calibration.
  """

  source: CameraFrame

  def __post_init__(self) -> None:
    """Restrict the recovered fixed-size algorithm to the reference camera geometry."""
    if self.source.geometry != CameraGeometry(321, 245):
      raise ValueError("Optical-black correction requires the reference 321 by 245 transfer")

  @property
  def offset(self) -> int:
    """Truncated mean of the 640 optical-black reference pixels, in camera counts."""
    opb = CameraFrame(self.source.geometry, self.source.wire_data, include_optical_black=True)
    return int(sum(struct.unpack_from("<640H", opb.image_bytes)) // 640)

  @property
  def image_bytes(self) -> bytes:
    """Return the native corrected 320 by 240 image as little-endian uint16 pixels."""
    opb = CameraFrame(self.source.geometry, self.source.wire_data, include_optical_black=True)
    pixels = struct.unpack("<78400H", opb.image_bytes)
    offset = sum(pixels[:640]) // 640
    return struct.pack(
      "<76800H", *(max(0, value - offset + 128) & 0xFFFF for value in pixels[1600:])
    )


class _MaskReader:
  """Read the bounded MFC class/object stream used by the reference PlateMask files."""

  def __init__(self, data: bytes) -> None:
    """Keep object numbering separate from class references, as in CArchive."""
    self.data = data
    self.position = 0
    self.classes: dict[int, tuple[bytes, int]] = {}
    self.next_index = 1

  def take(self, size: int) -> bytes:
    """Read exactly size bytes or reject a truncated archive."""
    end = self.position + size
    if size < 0 or end > len(self.data):
      raise ABI7500ProtocolError("Truncated pixel mask")
    result = self.data[self.position : end]
    self.position = end
    return result

  def word(self) -> int:
    """Read an unsigned little-endian archive word."""
    return int.from_bytes(self.take(2), "little")

  def object(self, name: bytes, schema: int) -> None:
    """Accept a supported new class or its class reference, never an object alias."""
    tag = self.word()
    if tag == 0xFFFF:
      version = self.word()
      length = self.word()
      if length > 64:
        raise ABI7500ProtocolError("Invalid pixel-mask class name length")
      actual = (self.take(length), version)
      self.classes[self.next_index] = actual
      self.next_index += 1
    elif tag & 0x8000 and (tag & 0x7FFF) in self.classes:
      actual = self.classes[tag & 0x7FFF]
    else:
      raise ABI7500ProtocolError("Unsupported pixel-mask object reference")
    if actual != (name, schema):
      raise ABI7500ProtocolError("Unsupported pixel-mask class or schema")
    self.next_index += 1

  def well(self) -> Tuple[Tuple[int, int], ...]:
    """Decode the selected (row, column) pixels, preserving native iterator order."""
    self.object(b"CMNImageAOI", 2)
    self.take(52)  # Saved center/dimensions; the outline and matrix determine selected pixels.
    self.take(94)  # Serialized statistics; not live measurements or correction coefficients.
    capacity, count = self.word(), self.word()
    if not 1 <= count <= capacity <= 4096:
      raise ABI7500ProtocolError("Invalid pixel-mask outline size")
    points = [struct.unpack("<ii", self.take(8)) for _ in range(count)]
    if any(not (0 <= row < 240 and 0 <= col < 320) for row, col in points):
      raise ABI7500ProtocolError("Pixel-mask outline extends beyond the reference image")
    row_min = min(point[0] for point in points)
    col_min = min(point[1] for point in points)
    self.object(b"CMNImageMatrix", 1)
    header = struct.unpack("<IIIIHiiHiHHHHii", self.take(48))
    row_capacity, rows, col_capacity, cols, size, byte_mode, word_mode = header[:7]
    if not (1 <= rows <= row_capacity <= 240 and 1 <= cols <= col_capacity <= 320):
      raise ABI7500ProtocolError("Invalid pixel-mask matrix dimensions")
    if (size, byte_mode, word_mode) != (2, 0, 1):
      raise ABI7500ProtocolError("Unsupported pixel-mask matrix storage")
    if row_min + rows > 240 or col_min + cols > 320:
      raise ABI7500ProtocolError("Pixel-mask matrix extends beyond the reference image")
    if (rows, cols) != (
      max(point[0] for point in points) - row_min + 1,
      max(point[1] for point in points) - col_min + 1,
    ):
      raise ABI7500ProtocolError("Pixel-mask matrix and outline dimensions disagree")
    pixels = struct.unpack(f"<{rows * cols}H", self.take(2 * rows * cols))
    selected = tuple(
      (row_min + i // cols, col_min + i % cols) for i, value in enumerate(pixels) if value
    )
    if not selected:
      raise ABI7500ProtocolError("A well pixel mask cannot be empty")
    return selected


def _read_generated_mask(data: bytes, filter: int) -> Tuple[Tuple[Tuple[int, int], ...], ...]:
  """Decode a PLR pixel mask, rejecting overlaps, duplicates and incomplete regions."""
  reader = _MaskReader(data)
  reader.take(len(_PLR_MASK_HEADER))
  if reader.take(1)[0] != filter:
    raise ABI7500ProtocolError("Generated mask filter does not match its requested filter")
  reader.take(32)  # SHA-256 of the unmodified ROI image in vendor coordinates.
  wells = []
  occupied: set[tuple[int, int]] = set()
  for _ in range(96):
    count = reader.word()
    if not 1 <= count <= 4096:
      raise ABI7500ProtocolError("Invalid generated well size")
    points = tuple(struct.unpack("<BH", reader.take(3)) for _ in range(count))
    if (
      any(row >= 240 or column >= 320 for row, column in points)
      or points != tuple(sorted(set(points)))
      or occupied.intersection(points)
    ):
      raise ABI7500ProtocolError("Generated mask has invalid, unordered or overlapping pixels")
    occupied.update(points)
    wells.append(points)
  if reader.position != len(data):
    raise ABI7500ProtocolError("Unexpected bytes after generated mask")
  return tuple(wells)


@dataclass(frozen=True)
class PlatePixelMask:
  """A vendor AOI archive or PLR-generated mask with an acquisition filter index.

  Vendor AOI files have no camera or filter identity that this parser can verify; ``filter``
  (1–5) is the caller's association. The reference F0 file corresponds to acquisition filter 1.
  PLR-generated masks embed their filter and source-image hash. Neither format establishes
  current calibration validity or verifies the instrument/camera association.
  """

  source_bytes: bytes
  filter: int

  @classmethod
  def from_file(cls, path: Path, *, filter: int) -> "PlatePixelMask":
    """Read a selected mask, refusing oversized input before allocating the full archive."""
    with path.open("rb") as stream:
      return cls(stream.read(1024 * 1024 + 1), filter)

  @lru_cache(maxsize=16)
  def _decode(self) -> Tuple[Tuple[Tuple[int, int], ...], ...]:
    """Decode the supported vendor archive or versioned PLR mask format."""
    if self.source_bytes.startswith(_PLR_MASK_HEADER):
      return _read_generated_mask(self.source_bytes, self.filter)
    reader = _MaskReader(self.source_bytes)
    reader.object(b"PlateMask", 10)
    if reader.word() != 96:
      raise ABI7500ProtocolError("Expected exactly 96 well masks")
    wells = tuple(reader.well() for _ in range(96))
    reader.take(30)  # Preserve the footer; its extraction settings are not applied here.
    if reader.position != len(self.source_bytes):
      raise ABI7500ProtocolError("Unexpected bytes after the pixel mask")
    return wells

  def __post_init__(self) -> None:
    """Validate immutable input, an explicit filter association, and the complete mask."""
    _check_unsigned("filter", self.filter, 5)
    if self.filter == 0:
      raise ValueError("Well masks require acquisition filter 1 through 5")
    if not isinstance(self.source_bytes, bytes) or len(self.source_bytes) > 1024 * 1024:
      raise ValueError("Pixel-mask input must be immutable bytes no larger than 1 MiB")
    self._decode()

  @classmethod
  def from_regions(
    cls,
    wells: Tuple[Tuple[Tuple[int, int], ...], ...],
    *,
    filter: int,
    source_image_sha256: str,
  ) -> "PlatePixelMask":
    """Encode 96 ordered regions in PLR's format, which is not a vendor calibration file.

    Regions must contain unique row-major pixels and must not overlap. Their order is
    A1 through H12; the caller must establish that order from the image geometry.
    The image hash identifies the original 320 by 240 uint16 image in vendor coordinates.
    """
    _check_unsigned("filter", filter, 5)
    if filter == 0:
      raise ValueError("Well masks require acquisition filter 1 through 5")
    if (
      not isinstance(source_image_sha256, str)
      or len(source_image_sha256) != 64
      or any(c not in "0123456789abcdef" for c in source_image_sha256)
    ):
      raise ValueError("Source image SHA-256 must be 64 lowercase hexadecimal characters")
    if type(wells) is not tuple or len(wells) != 96:
      raise ValueError("Generated mask requires 96 immutable well regions")
    data = bytearray(_PLR_MASK_HEADER + bytes([filter]) + bytes.fromhex(source_image_sha256))
    for points in wells:
      if type(points) is not tuple or not 1 <= len(points) <= 4096:
        raise ValueError("Generated regions require 1 to 4096 immutable points")
      data.extend(struct.pack("<H", len(points)))
      for point in points:
        if (
          type(point) is not tuple
          or len(point) != 2
          or any(type(v) is not int for v in point)
          or not (0 <= point[0] < 240 and 0 <= point[1] < 320)
        ):
          raise ValueError("Generated mask points must be integer image coordinates")
        data.extend(struct.pack("<BH", *point))
    return cls(bytes(data), filter)

  @property
  def generated(self) -> bool:
    """Whether the stored bytes use PLR's generated-mask format rather than vendor AOI."""
    return self.source_bytes.startswith(_PLR_MASK_HEADER)

  @property
  def source_image_sha256(self) -> Optional[str]:
    """Return the generating image hash; vendor AOI files do not provide this field."""
    start = len(_PLR_MASK_HEADER) + 1
    return self.source_bytes[start : start + 32].hex() if self.generated else None

  @property
  def sha256(self) -> str:
    """Hash of the original mask, including unused settings and saved statistics."""
    return hashlib.sha256(self.source_bytes).hexdigest()

  @property
  def wells(self) -> Tuple[Tuple[Tuple[int, int], ...], ...]:
    """Selected pixels for each well in saved mask order, without a geometric approximation."""
    return self._decode()


@dataclass(frozen=True)
class WellPixelMeasurement:
  """Diagnostic counts over one mask, without exposure scaling or fluorescence calibration."""

  well: int
  pixel_count: int
  pixel_sum: int
  minimum: int
  maximum: int

  @property
  def label(self) -> str:
    """Vendor mask-order label; physical plate orientation requires separate validation."""
    return f"{chr(65 + (self.well - 1) // 12)}{(self.well - 1) % 12 + 1}"

  @property
  def mean(self) -> float:
    """Mean camera counts, including zero-valued pixels inside the mask."""
    return self.pixel_sum / self.pixel_count


@dataclass(frozen=True)
class CameraWellData:
  """Diagnostic per-well pixels with the original acquisition and mask retained.

  Explicitly select ``none`` for the ArpLib OPB=0 image, or ``optical_black`` for the HAMCamera
  corrected OPB=1 image. Both rotate 180 degrees into the vendor mask coordinates; their
  horizontal copy origins differ by one pixel. These statistics are
  not interchangeable with SavedFilterData and must not be passed to apply_uniformity.
  """

  source: CameraAcquisition
  mask: PlatePixelMask
  correction: Literal["none", "optical_black"]

  def __post_init__(self) -> None:
    """Require complete, coherent acquisition metadata and matching filter indices."""
    if self.correction not in ("none", "optical_black"):
      raise ValueError("Unknown camera pixel correction")
    if self.source.state != "complete" or self.source.frame is None:
      raise ValueError("Well extraction requires a complete camera acquisition")
    if self.source.geometry != CameraGeometry(321, 245):
      raise ValueError("Pixel masks require the reference 321 by 245 camera geometry")
    if self.source.optics_after != self.source.optics_before:
      raise ValueError("Optical state changed during the camera acquisition")
    if (
      not self.source.optics_before.is_stationary
      or self.source.optics_before.filter_position != self.mask.filter
    ):
      raise ValueError("Pixel mask does not match the stationary acquisition filter")

  @property
  def measurements(self) -> Tuple[WellPixelMeasurement, ...]:
    """Compute exact sums over saved masks, without thresholding, normalization or unmixing."""
    frame = self.source.frame
    assert frame is not None
    image = (
      OpticalBlackImage(frame).image_bytes
      if self.correction == "optical_black"
      else frame.image_bytes
    )
    pixels = struct.unpack("<76800H", image)[::-1]
    results = []
    for well, mask in enumerate(self.mask.wells, 1):
      values = [pixels[row * 320 + col] for row, col in mask]
      results.append(WellPixelMeasurement(well, len(values), sum(values), min(values), max(values)))
    return tuple(results)
