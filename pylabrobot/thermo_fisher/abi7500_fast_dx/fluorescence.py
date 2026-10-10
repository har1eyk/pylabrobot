"""Reconstruct reference 7500 filter signals from camera frames and explicit calibration files.

This is the recorded OPB=0, sum-extraction, one-pixel-ring path. It does not establish current
calibration validity, identify dyes, execute synchronized runs, or calculate Ct values.
"""

import hashlib
import math
import struct
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Tuple, Union

from .acquisition import CameraExposure
from .calibration import UniformityCalibration, _coefficient, _float32, _integer_float32
from .camera import CameraFrame, CameraGeometry
from .exposure_selection import ExposureSelection
from .image_processing import PlatePixelMask


def _background_number(text: str) -> float:
  """Read a signed finite decimal directly into the vendor's binary32 coefficient format."""
  try:
    value = Decimal(text)
  except InvalidOperation as exc:
    raise ValueError("Invalid background coefficient") from exc
  if not value.is_finite():
    raise ValueError("Background coefficients must be finite")
  if value.is_zero():
    return 0.0
  magnitude = _coefficient(str(value.copy_abs()))
  return -magnitude if value < 0 else magnitude


@dataclass(frozen=True)
class BackgroundCalibration:
  """The native version-5 Background.csv, retaining its original bytes and SHA-256.

  This file has no instrument identity, expiry or pass flag. Select it from the matching backup;
  it cannot certify current calibration. The EDS background.ini has lower decimal precision and
  is deliberately not accepted as a replacement for exact native reconstruction.
  """

  source_bytes: bytes

  def _rows(self) -> tuple[tuple[float, ...], ...]:
    """Validate the reference native CSV layout and return five offsets then five slopes."""
    if not isinstance(self.source_bytes, bytes) or len(self.source_bytes) > 1024 * 1024:
      raise ValueError("Expected immutable background CSV bytes, at most 1 MiB")
    lines = self.source_bytes.decode("utf-8-sig").splitlines()
    if (
      len(lines) != 13
      or lines[0] != "Background Calibration, version = 5"
      or lines[1] != "[Offsets] rows = Filters A B C D E: columns = wells"
      or lines[7]
      != "[slopes], from formula for water subtraction [(Dye-offset)*VE/exp] - "
      "[(slope*VE)]: Rows = Filters A B C D E: columns = wells"
    ):
      raise ValueError("Unsupported native background CSV layout")
    rows = []
    for line in lines[2:7] + lines[8:13]:
      fields = line.split(",")
      if len(fields) != 97 or fields[-1] != "":
        raise ValueError("Background CSV requires five rows of 96 offsets and 96 slopes")
      rows.append(tuple(_background_number(value) for value in fields[:-1]))
    return tuple(rows)

  def __post_init__(self) -> None:
    """Reject unsupported data before it can enter image processing."""
    self._rows()

  @property
  def offsets(self) -> tuple[tuple[float, ...], ...]:
    """Five offset arrays in saved-filter order, then row-major wells 1–96."""
    return self._rows()[:5]

  @property
  def slopes(self) -> tuple[tuple[float, ...], ...]:
    """Five slope arrays in saved-filter order, then row-major wells 1–96."""
    return self._rows()[5:]

  @property
  def sha256(self) -> str:
    """Identify the exact calibration file; this does not certify instrument association."""
    return hashlib.sha256(self.source_bytes).hexdigest()

  @classmethod
  def from_file(cls, path: Union[str, Path]) -> "BackgroundCalibration":
    """Read an explicitly selected native calibration without changing it."""
    return cls(Path(path).read_bytes())


@dataclass(frozen=True)
class ExtractedWell:
  """Native intermediate counts, including the original sum and surrounding-pixel correction."""

  well: int
  pixel_sum: int
  pixel_count: int
  ring_sum: int
  ring_count: int
  saturation_count: int

  @property
  def raw_value(self) -> int:
    """Subtract the ring mean, clip below zero and truncate as in the native run buffer."""
    return max(0, int(self.pixel_sum - self.ring_sum / self.ring_count * self.pixel_count))

  @property
  def saturated(self) -> bool:
    """Whether more than six selected pixels exceeded the native threshold of 4000."""
    return self.saturation_count > 6


@dataclass(frozen=True)
class FluorescenceFrame:
  """One original camera frame, its commanded exposure and explicitly associated pixel mask.

  The supported path uses OPB=0, rotates 180° into the vendor mask coordinates, sums nonzero
  selected pixels and subtracts the mean of nonzero pixels in the adjacent eight-neighbor ring.
  All original source bytes remain available. This object performs no hardware I/O.
  """

  source: CameraFrame
  exposure: CameraExposure
  mask: PlatePixelMask

  @property
  def image_bytes(self) -> bytes:
    """The vendor's OPB=0 crop in mask coordinates, as little-endian uint16 pixels."""
    frame = CameraFrame(self.source.geometry, self.source.wire_data)
    return struct.pack("<76800H", *reversed(struct.unpack("<76800H", frame.image_bytes)))

  def __post_init__(self) -> None:
    """Limit extraction to the recovered complete 12-bit reference image and bounded rings."""
    if self.source.geometry != CameraGeometry(321, 245):
      raise ValueError("Fluorescence extraction requires the reference camera geometry")
    if max(struct.unpack("<76800H", self.image_bytes)) > 4095:
      raise ValueError("Fluorescence extraction supports the recorded 12-bit camera path only")
    if any(not 0 < r < 239 or not 0 < c < 319 for mask in self.mask.wells for r, c in mask):
      raise ValueError("The native one-pixel ring must fit entirely inside the image")

  @property
  def wells(self) -> tuple[ExtractedWell, ...]:
    """Extract all 96 regions, keeping saturation and ring statistics separately."""
    pixels = struct.unpack("<76800H", self.image_bytes)
    wells = []
    for index, mask in enumerate(self.mask.wells, 1):
      selected = set(mask)
      ring = {
        (row + dr, col + dc) for row, col in mask for dr in (-1, 0, 1) for dc in (-1, 0, 1)
      } - selected
      values = [pixels[row * 320 + col] for row, col in mask if pixels[row * 320 + col]]
      surrounding = [pixels[row * 320 + col] for row, col in ring if pixels[row * 320 + col]]
      if not values or not surrounding:
        raise ValueError("Native extraction requires nonzero well and ring pixels")
      wells.append(
        ExtractedWell(
          index,
          sum(values),
          len(values),
          sum(surrounding),
          len(surrounding),
          sum(value > 4000 for value in values),
        )
      )
    return tuple(wells)


@dataclass(frozen=True)
class CameraWellMeasurement:
  """One reconstructed filter signal with exposure choice and saturation provenance."""

  well: int
  extraction: Tuple[ExtractedWell, ExtractedWell, ExtractedWell]
  selection: ExposureSelection
  uniformity_coefficient: float

  @property
  def label(self) -> str:
    """Vendor row-major label A1–H12, without an independent physical plate orientation claim."""
    return f"{chr(65 + (self.well - 1) // 12)}{(self.well - 1) % 12 + 1}"

  @property
  def raw_value(self) -> int:
    """The reconstructed pre-uniformity vendor integer, not an uncorrected pixel sum."""
    return self.selection.value

  @property
  def corrected_value(self) -> float:
    """Uniformity-corrected binary32 filter signal; not dye concentration, Rn, or Ct."""
    return _float32(_integer_float32(self.raw_value) * self.uniformity_coefficient)

  @property
  def selected_saturated(self) -> bool:
    """Flag fallback to a saturated exposure instead of presenting it as an unsaturated value."""
    return self.selection.saturated[self.selection.selected_index]


@dataclass(frozen=True)
class CameraFilterResult:
  """Reconstruct one three-exposure collection point with explicit calibration provenance.

  The caller must associate frames from the same filter and collection point, and supply that
  point's stage/cycle metadata. Calibration files do not bind themselves to this instrument or
  prove present validity. This class does not trigger or synchronize acquisition.
  """

  frames: Tuple[FluorescenceFrame, FluorescenceFrame, FluorescenceFrame]
  background: BackgroundCalibration
  uniformity: UniformityCalibration
  stage: int
  cycle: int
  collection_point: int

  def __post_init__(self) -> None:
    """Reject mixed masks, duplicate exposure times and missing collection-point metadata."""
    if type(self.frames) is not tuple or len(self.frames) != 3:
      raise ValueError("Exactly three immutable exposure frames are required")
    if any(frame.mask != self.frames[0].mask for frame in self.frames):
      raise ValueError("All three frames must use the same mask and physical filter")
    if len({frame.exposure.milliseconds for frame in self.frames}) != 3:
      raise ValueError("Three distinct commanded exposure times are required")
    if any(
      type(index) is not int or index < 1
      for index in (self.stage, self.cycle, self.collection_point)
    ):
      raise ValueError("Stage, cycle and collection point must be positive integers")

  @property
  def filter(self) -> int:
    """Saved-spectrum filter index; the vendor reverses the five physical filter positions."""
    return 6 - self.frames[0].mask.filter

  @property
  def measurements(self) -> tuple[CameraWellMeasurement, ...]:
    """Apply the recorded background, exposure selection and uniformity stages exactly once."""
    extracted = [frame.wells for frame in self.frames]
    offsets = self.background.offsets[self.filter - 1]
    slopes = self.background.slopes[self.filter - 1]
    factors = self.uniformity.coefficients[self.filter - 1]
    durations = tuple(frame.exposure.milliseconds / 1000 for frame in self.frames)
    results = []
    for well in range(96):
      normalized = []
      regions = (extracted[0][well], extracted[1][well], extracted[2][well])
      for frame, region in zip(self.frames, regions):
        effective = math.ceil((frame.exposure.milliseconds + 0.2746) / 0.1702) * 0.1702
        value = 1000 * (region.raw_value - offsets[well]) / effective - 1000 * slopes[well]
        normalized.append(min(0xFFFFFFFF, max(0, int(value))))
      selection = ExposureSelection(
        (durations[0], durations[1], durations[2]),
        (normalized[0], normalized[1], normalized[2]),
        (regions[0].saturated, regions[1].saturated, regions[2].saturated),
      )
      results.append(CameraWellMeasurement(well + 1, regions, selection, factors[well]))
    return tuple(results)
