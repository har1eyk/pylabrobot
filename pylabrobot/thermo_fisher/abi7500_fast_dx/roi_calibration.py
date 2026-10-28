"""Generate 96-well pixel masks from ROI images without vendor masks or instrument I/O."""

import hashlib
import struct
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .camera import CameraFrame, CameraGeometry
from .image_processing import PlatePixelMask
from .protocol import _check_unsigned

_HEIGHT, _WIDTH = 240, 320
Region = tuple[tuple[int, int], ...]


def _threshold(values: tuple[int, ...], shift: int = 0, *, reject_unbalanced: bool = False) -> int:
  """Select the last maximum of between-class variance on the quantized histogram."""
  histogram = [0] * (4096 >> shift)
  for value in values:
    histogram[value >> shift] += 1
  occupied = [i for i, count in enumerate(histogram) if count]
  if len(occupied) < 2:
    return 0
  probability = [count / len(values) for count in histogram]
  total = 0.0
  for i, probability_at_i in enumerate(probability):
    total += (i + 1) * probability_at_i
  weight = moment = 0.0
  best = -1.0
  threshold = 0
  for i in range(occupied[0], occupied[-1] + 1):
    weight += probability[i]
    moment += (i + 1) * probability[i]
    denominator = weight * (1 - weight)
    variance = (weight * total - moment) ** 2 / denominator if denominator > 0 else 0
    if variance >= best:
      best, threshold = variance, i
  if reject_unbalanced:
    # The native valley test uses peak indices from a histogram with a leading zero,
    # then looks them up in the original histogram. Keep that one-bin offset.
    lower_peak = max(range(occupied[0], threshold), key=histogram.__getitem__, default=0) + 1
    upper_peak = max(range(threshold, occupied[-1] + 1), key=histogram.__getitem__) + 1
    low_count, high_count = sum(histogram[:threshold]), sum(histogram[threshold:])
    if (
      upper_peak < len(histogram)
      and histogram[threshold] < min(histogram[lower_peak], histogram[upper_peak])
      and (low_count < 0.05 * high_count or low_count > 20 * high_count)
    ):
      return 0
  return (threshold << shift) + ((1 << (shift - 1)) if shift else 0)


def _threshold_grid(pixels: tuple[int, ...], global_threshold: int) -> tuple[tuple[int, ...], ...]:
  """Calculate a three-level quadtree and extrapolate its 8 by 8 leaf thresholds."""
  rectangles = [(0, 0, _WIDTH, _HEIGHT)]
  thresholds = [_threshold(pixels)]
  for parent in range(21):
    x0, y0, x1, y1 = rectangles[parent]
    xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
    for top, bottom in ((y0, ym), (ym, y1)):
      for left, right in ((x0, xm), (xm, x1)):
        rectangles.append((left, top, right, bottom))
        region = tuple(
          pixels[row * _WIDTH + column]
          for row in range(top, bottom)
          for column in range(left, right)
        )
        thresholds.append(_threshold(region, reject_unbalanced=True))
  median = sorted(thresholds)[42]
  low, high = median - int(median * 0.3), median + int(median * 0.5)
  thresholds = [value if low <= value <= high else 0 for value in thresholds]
  for parent in range(21):
    children = range(4 * parent + 1, 4 * parent + 5)
    retained = [thresholds[i] for i in children if thresholds[i]]
    replacement = sum(retained) // 3 if len(retained) == 3 else thresholds[parent]
    for index in children:
      if thresholds[index] == 0:
        thresholds[index] = replacement
  delta = thresholds[0] - global_threshold
  grid = [[0] * 10 for _ in range(10)]
  for (left, top, _, _), threshold in zip(rectangles[21:], thresholds[21:]):
    grid[top // 30 + 1][left // 40 + 1] = (threshold - delta) & 0xFFFF
  for index in range(1, 9):
    grid[0][index] = 2 * grid[1][index] - grid[2][index]
    grid[9][index] = 2 * grid[8][index] - grid[7][index]
    grid[index][0] = 2 * grid[index][1] - grid[index][2]
    grid[index][9] = 2 * grid[index][8] - grid[index][7]
  for row, inner, other in ((0, 1, 2), (9, 8, 7)):
    for column, inner_column, other_column in ((0, 1, 2), (9, 8, 7)):
      grid[row][column] = 2 * grid[inner][inner_column] - grid[other][other_column]
  return tuple(tuple(value & 0xFFFF for value in row) for row in grid)


def _segment(pixels: tuple[int, ...], grid: tuple[tuple[int, ...], ...]) -> set[int]:
  """Interpolate thresholds, repair horizontal gaps and remove isolated foreground pixels."""
  selected: set[int] = set()
  for row in range(_HEIGHT):
    gr, dy = divmod(row + 15, 30)
    for column in range(_WIDTH):
      gc, dx = divmod(column + 20, 40)
      threshold = (
        (30 - dy) * (40 - dx) * grid[gr][gc]
        + (30 - dy) * dx * grid[gr][gc + 1]
        + dy * (40 - dx) * grid[gr + 1][gc]
        + dy * dx * grid[gr + 1][gc + 1]
      ) // 1200
      index = row * _WIDTH + column
      if pixels[index] > threshold:
        selected.add(index)
  for row in range(1, _HEIGHT - 1):
    for column in range(1, _WIDTH - 1):
      index = row * _WIDTH + column
      if index - 1 in selected and index + 1 in selected:
        selected.add(index)
  selected = {i for i in selected if 0 < i // _WIDTH < 239 and 0 < i % _WIDTH < 319}
  neighbors = (-321, -320, -319, -1, 1, 319, 320, 321)
  for _ in range(3):
    changed = False
    for index in sorted(selected):
      if sum(index + step in selected for step in neighbors) < 2:
        selected.remove(index)
        changed = True
    if not changed:
      break
  for _ in range(2):
    selected = {
      (row + dr) * _WIDTH + column + dc
      for index in selected
      for row, column in (divmod(index, _WIDTH),)
      for dr in (-1, 0, 1)
      for dc in (-1, 0, 1)
      if 0 < row + dr < 239 and 0 < column + dc < 319
    }
  return selected


def _components(selected: set[int]) -> tuple[Region, ...]:
  """Collect connected foreground regions without substituting a nominal well grid."""
  remaining = set(selected)
  regions = []
  while remaining:
    seed = min(remaining)
    remaining.remove(seed)
    pending, found = [seed], [seed]
    while pending:
      index = pending.pop()
      row, column = divmod(index, _WIDTH)
      for dr, dc in ((-1, 0), (0, -1), (0, 1), (1, 0)):
        r, c = row + dr, column + dc
        neighbor = r * _WIDTH + c
        if 0 <= r < _HEIGHT and 0 <= c < _WIDTH and neighbor in remaining:
          remaining.remove(neighbor)
          pending.append(neighbor)
          found.append(neighbor)
    regions.append(tuple(divmod(i, _WIDTH) for i in sorted(found)))
  return tuple(regions)


def _bounds(region: Region) -> tuple[int, int, int, int]:
  """Return inclusive image bounds for a nonempty region."""
  return (
    min(r for r, _ in region),
    min(c for _, c in region),
    max(r for r, _ in region),
    max(c for _, c in region),
  )


def _fill_region(region: Region) -> Region:
  """Fill enclosed holes while retaining the detected outer boundary."""
  top, left, bottom, right = _bounds(region)
  points = set(region)
  exterior = {
    (row, column)
    for row in range(top, bottom + 1)
    for column in range(left, right + 1)
    if (row in (top, bottom) or column in (left, right)) and (row, column) not in points
  }
  pending = list(exterior)
  while pending:
    row, column = pending.pop()
    for dr, dc in ((-1, 0), (0, -1), (0, 1), (1, 0)):
      r, c = row + dr, column + dc
      if (
        top <= r <= bottom
        and left <= c <= right
        and (r, c) not in points
        and (r, c) not in exterior
      ):
        exterior.add((r, c))
        pending.append((r, c))
  return tuple(
    (row, column)
    for row in range(top, bottom + 1)
    for column in range(left, right + 1)
    if (row, column) not in exterior
  )


def _center(region: Region) -> tuple[float, float]:
  """Return the center of the bounding rectangle, matching native ordering coordinates."""
  top, left, bottom, right = _bounds(region)
  return (top + bottom + 1) / 2, (left + right + 1) / 2


def _ordered_regions(regions: tuple[Region, ...]) -> tuple[tuple[Region, ...], tuple[str, ...]]:
  """Require an unambiguous eight-row, twelve-column plate before assigning well labels."""
  if len(regions) != 96:
    return (), (f"Expected 96 regions; found {len(regions)}",)
  by_row = sorted(regions, key=lambda region: _center(region)[0])
  rows = [
    sorted(by_row[i : i + 12], key=lambda region: _center(region)[1]) for i in range(0, 96, 12)
  ]
  for row in rows:
    centers = [_center(region) for region in row]
    pitch = sorted(centers[i + 1][1] - centers[i][1] for i in range(11))[5]
    if pitch <= 0 or max(y for y, _ in centers) - min(y for y, _ in centers) > pitch / 2:
      return (), ("Regions do not form twelve separated wells per image row",)
    for left_region, right_region in zip(row, row[1:]):
      if _bounds(left_region)[3] >= _bounds(right_region)[1]:
        return (), ("Neighboring well bounds overlap horizontally",)
  for upper, lower in zip(rows, rows[1:]):
    for above, below in zip(upper, lower):
      if _bounds(above)[2] >= _bounds(below)[0]:
        return (), ("Neighboring well bounds overlap vertically",)
  ordered = tuple(region for row in rows for region in row)
  for region in ordered:
    top, left, bottom, right = _bounds(region)
    if top < 4 or left < 4 or bottom >= 236 or right >= 316 or len(region) > 4096:
      return (), ("A well extends outside the supported image margin or size",)
    points = set(region)
    perimeter = sum(
      (r + dr, c + dc) not in points
      for r, c in region
      for dr, dc in ((-1, 0), (0, -1), (0, 1), (1, 0))
    )
    if not 50 <= perimeter <= 150:
      return (), ("A well boundary is outside the supported size range",)
  return ordered, ()


@dataclass(frozen=True)
class ROICalibrationImage:
  """Uncorrected 320 by 240 uint16 image in vendor TIFF coordinates and physical filter.

  ``image_bytes`` is little-endian, row-major, with values from the 12-bit C9254 path.
  Do not supply TIFF container bytes or apply background/uniformity correction first.
  ``from_camera`` converts the supported raw camera geometry and rotates it 180 degrees.
  """

  image_bytes: bytes
  filter: int

  def __post_init__(self) -> None:
    """Reject malformed image buffers and unsupported filter or camera count ranges."""
    _check_unsigned("filter", self.filter, 5)
    if self.filter == 0:
      raise ValueError("ROI calibration requires physical filter 1 through 5")
    if type(self.image_bytes) is not bytes or len(self.image_bytes) != 153600:
      raise ValueError("ROI image requires exactly 320 by 240 little-endian uint16 pixels")
    if max(struct.unpack("<76800H", self.image_bytes)) > 4095:
      raise ValueError("ROI image exceeds the supported 12-bit camera range")

  @classmethod
  def from_camera(cls, frame: CameraFrame, *, filter: int) -> "ROICalibrationImage":
    """Crop the original transfer with OPB off, then rotate pixels into vendor coordinates."""
    if not isinstance(frame, CameraFrame) or frame.geometry != CameraGeometry(321, 245):
      raise ValueError("ROI calibration requires a complete 321 by 245 C9254 camera frame")
    raw = CameraFrame(frame.geometry, frame.wire_data, include_optical_black=False)
    values = struct.unpack("<76800H", raw.image_bytes)
    return cls(struct.pack("<76800H", *reversed(values)), filter)

  @property
  def sha256(self) -> str:
    """Hash the original, unmodified ROI pixel bytes."""
    return hashlib.sha256(self.image_bytes).hexdigest()


@dataclass(frozen=True)
class ROIMaskResult:
  """Derived regions, segmentation inputs and quality outcome for one image.

  Passing describes image geometry only. It does not verify plate orientation, calibration
  plate chemistry, instrument condition, exposure selection or a vendor calibration status.
  """

  image: ROICalibrationImage
  threshold: int
  threshold_grid: tuple[tuple[int, ...], ...]
  regions: tuple[Region, ...]
  failure_reasons: tuple[str, ...]

  def __post_init__(self) -> None:
    """Require immutable diagnostics and a complete ordered grid for a passing result."""
    if not isinstance(self.image, ROICalibrationImage):
      raise TypeError("ROI result requires its original ROICalibrationImage")
    _check_unsigned("threshold", self.threshold, 4095)
    if type(self.failure_reasons) is not tuple or any(
      type(reason) is not str or not reason for reason in self.failure_reasons
    ):
      raise ValueError("ROI failure reasons must be an immutable tuple of nonempty strings")
    if type(self.regions) is not tuple or any(
      type(region) is not tuple
      or not region
      or any(
        type(point) is not tuple
        or len(point) != 2
        or any(type(value) is not int for value in point)
        or not (0 <= point[0] < 240 and 0 <= point[1] < 320)
        for point in region
      )
      for region in self.regions
    ):
      raise ValueError("ROI regions must contain immutable image coordinates")
    if type(self.threshold_grid) is not tuple or (
      self.threshold_grid
      and (
        len(self.threshold_grid) != 10
        or any(
          type(row) is not tuple
          or len(row) != 10
          or any(type(value) is not int or not 0 <= value <= 65535 for value in row)
          for row in self.threshold_grid
        )
      )
    ):
      raise ValueError("ROI thresholds require a 10 by 10 immutable uint16 grid")
    if not self.failure_reasons:
      ordered, reasons = _ordered_regions(self.regions)
      if not self.threshold_grid or reasons or ordered != self.regions:
        raise ValueError("A passing ROI result requires 96 ordered regions and threshold grid")

  @property
  def passed(self) -> bool:
    """Whether this image produced a geometrically acceptable 96-well mask."""
    return not self.failure_reasons

  @cached_property
  def mask(self) -> PlatePixelMask:
    """Generate a portable PLR mask for the existing well-measurement pipeline."""
    if not self.passed:
      raise ValueError("ROI mask generation failed: " + "; ".join(self.failure_reasons))
    return PlatePixelMask.from_regions(
      self.regions, filter=self.image.filter, source_image_sha256=self.image.sha256
    )

  @property
  def saturated_pixels(self) -> int:
    """Count clipped pixels in the original image, including pixels outside well regions."""
    return sum(value == 4095 for value in struct.unpack("<76800H", self.image.image_bytes))

  def report(self) -> dict[str, Any]:
    """Describe provenance, thresholding and region geometry without image payloads."""
    return {
      "schema": "plr-abi7500-roi-v1",
      "physical_filter": self.image.filter,
      "source_image_sha256": self.image.sha256,
      "image_dimensions": [320, 240],
      "coordinate_system": "vendor TIFF; OPB=0 crop rotated 180 degrees",
      "threshold": self.threshold,
      "threshold_grid": [list(row) for row in self.threshold_grid],
      "passed": self.passed,
      "failure_reasons": list(self.failure_reasons),
      "saturated_pixels": self.saturated_pixels,
      "mask_sha256": self.mask.sha256 if self.passed else None,
      "regions": [
        {
          "well": f"{'ABCDEFGH'[i // 12]}{i % 12 + 1}" if self.passed else None,
          "pixels": len(region),
          "bounds": list(_bounds(region)),
        }
        for i, region in enumerate(self.regions)
      ],
    }


def generate_roi_mask(image: ROICalibrationImage) -> ROIMaskResult:
  """Find and order well regions from pixels alone; no prior masks or calibration are inputs.

  Original pixels and the interpolated threshold grid remain available in the result.
  Failure to find an unambiguous 8 by 12 geometry blocks mask export. This function performs
  no USB access, camera acquisition, calibration installation, or automatic file writes.
  """
  if not isinstance(image, ROICalibrationImage):
    raise TypeError("ROI mask generation requires ROICalibrationImage")
  pixels = struct.unpack("<76800H", image.image_bytes)
  threshold = _threshold(pixels, 2)
  if threshold < 300 or len(set(pixels)) < 2:
    return ROIMaskResult(image, threshold, (), (), ("Insufficient ROI image contrast",))
  segmentation_pixels = tuple(0 if value <= 10 else value for value in pixels)
  grid = _threshold_grid(segmentation_pixels, threshold)
  components = tuple(
    _fill_region(region) for region in _components(_segment(segmentation_pixels, grid))
  )
  regions, reasons = _ordered_regions(components)
  return ROIMaskResult(image, threshold, grid, regions if not reasons else components, reasons)
