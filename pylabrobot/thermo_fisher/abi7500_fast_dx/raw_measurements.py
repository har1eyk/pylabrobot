"""Offline, uncalibrated well measurements from explicitly supplied diagnostic image regions."""

import csv
import hashlib
import io
import math
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from pylabrobot.__version__ import __version__

from ._export import _json_bytes, _validate_collections
from .acquisition import CameraAcquisition
from .camera import CameraGeometry
from .collection import FluorescenceCollection
from .image_processing import WellPixelMeasurement
from .protocol import _check_unsigned
from .run import ThermalRun

_COORDINATES = "opb0-rotated-180"


@dataclass(frozen=True)
class DiagnosticPlateRegions:
  """Provisional regions for one camera and physical filter, without vendor calibration.

  ``wells`` contains 96 disjoint, nonempty tuples of integer (row, column) pixels in A1..H12
  order, in the 240-row by 320-column OPB=0 image rotated 180 degrees. These are caller-supplied
  associations, never inferred from brightness. A matching camera description guards against
  accidental cross-camera reuse; it does not prove optical alignment or current calibration.

  Args:
    camera_description: Exact recorded camera identity, including its serial number.
    filter: Physical filter position, 1..5.
    wells: Explicit selected pixels, including zero-valued image pixels in measurements.
    provenance: Source description and hashes sufficient to trace the region construction.
    verified_wells: One-based well indices whose physical labels were independently checked.
      This does not validate region boundaries or any other label.
    label_evidence: Description and hashes of that independent check, required for verified wells.
  """

  camera_description: str
  filter: int
  wells: tuple[tuple[tuple[int, int], ...], ...]
  provenance: str
  verified_wells: tuple[int, ...] = ()
  label_evidence: str = ""

  def __post_init__(self) -> None:
    """Reject ambiguous, mutable, overlapping or out-of-image region definitions."""
    _check_unsigned("filter", self.filter, 5)
    if self.filter == 0:
      raise ValueError("Diagnostic regions require physical filter 1 through 5")
    for value in (self.camera_description, self.provenance):
      if not isinstance(value, str) or not value.strip():
        raise ValueError("Diagnostic regions require camera identity and provenance")
    if type(self.wells) is not tuple or len(self.wells) != 96:
      raise ValueError("Provide exactly 96 immutable well regions in A1..H12 order")
    occupied: set[tuple[int, int]] = set()
    for well in self.wells:
      if type(well) is not tuple or not 1 <= len(well) <= 76800:
        raise ValueError("Each well requires a nonempty immutable pixel region")
      for pixel in well:
        if type(pixel) is not tuple or len(pixel) != 2:
          raise ValueError("Pixels must be immutable (row, column) pairs")
        row, column = pixel
        _check_unsigned("pixel row", row, 239)
        _check_unsigned("pixel column", column, 319)
        if pixel in occupied:
          raise ValueError("Diagnostic regions cannot duplicate or share pixels")
        occupied.add(pixel)
    if type(self.verified_wells) is not tuple:
      raise ValueError("Verified well indices must be immutable")
    for well_index in self.verified_wells:
      _check_unsigned("verified well", well_index, 96)
      if well_index == 0:
        raise ValueError("Verified wells use one-based indices")
    if len(set(self.verified_wells)) != len(self.verified_wells):
      raise ValueError("Duplicate verified well index")
    if not isinstance(self.label_evidence, str) or (
      self.verified_wells and not self.label_evidence.strip()
    ):
      raise ValueError("Verified labels require explicit independent evidence")

  @classmethod
  def from_circles(
    cls,
    centers: tuple[tuple[float, float], ...],
    radius: float,
    *,
    camera_description: str,
    filter: int,
    provenance: str,
    verified_wells: tuple[int, ...] = (),
    label_evidence: str = "",
  ) -> "DiagnosticPlateRegions":
    """Select pixels at squared distance <= radius squared from each supplied center.

    Centers and radius are in pixels in the rotated OPB=0 view. No fitting or image-dependent
    adjustment is performed. All circles must fit inside the image; overlapping pixels fail.
    """
    if type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0:
      raise ValueError("Circle radius must be finite and positive")
    if type(centers) is not tuple or len(centers) != 96:
      raise ValueError("Provide exactly 96 immutable circle centers")
    wells = []
    for center in centers:
      if type(center) is not tuple or len(center) != 2:
        raise ValueError("Circle centers must be (row, column) pairs")
      row, column = center
      if any(type(v) not in (int, float) or not math.isfinite(v) for v in center):
        raise ValueError("Circle centers must be finite numbers")
      if not (radius <= row <= 239 - radius and radius <= column <= 319 - radius):
        raise ValueError("Diagnostic circles must lie entirely inside the image")
      wells.append(
        tuple(
          (r, c)
          for r in range(math.ceil(row - radius), math.floor(row + radius) + 1)
          for c in range(math.ceil(column - radius), math.floor(column + radius) + 1)
          if (r - row) ** 2 + (c - column) ** 2 <= radius**2
        )
      )
    return cls(camera_description, filter, tuple(wells), provenance, verified_wells, label_evidence)

  def to_dict(self) -> dict[str, Any]:
    """Serialize explicit pixel membership and association evidence, never a vendor AOI file."""
    return {
      "format_version": 1,
      "coordinates": _COORDINATES,
      "regions_provisional": True,
      "camera_description": self.camera_description,
      "physical_filter": self.filter,
      "wells": [[list(pixel) for pixel in well] for well in self.wells],
      "provenance": self.provenance,
      "verified_wells": list(self.verified_wells),
      "label_evidence": self.label_evidence,
    }

  @classmethod
  def from_dict(cls, value: Mapping[str, Any]) -> "DiagnosticPlateRegions":
    """Restore explicit regions, rejecting unsupported coordinate or validity claims."""
    if (
      type(value["format_version"]) is not int
      or value["format_version"] != 1
      or value["coordinates"] != _COORDINATES
      or value["regions_provisional"] is not True
    ):
      raise ValueError("Unsupported diagnostic region format or coordinate convention")
    return cls(
      value["camera_description"],
      value["physical_filter"],
      tuple(tuple(cast(tuple[int, int], tuple(p)) for p in well) for well in value["wells"]),
      value["provenance"],
      tuple(value["verified_wells"]),
      value["label_evidence"],
    )

  @property
  def sha256(self) -> str:
    """Hash of canonical JSON including exact pixels, camera/filter and label evidence."""
    return hashlib.sha256(_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class RawWellMeasurement(WellPixelMeasurement):
  """Raw diagnostic counts, including zeros, without background or exposure correction.

  ``high_pixel_count`` counts values >4000; ``clipped_pixel_count`` counts values ==4095.
  Neither applies the vendor saturation decision to these differently sized regions.
  ``label_verified`` refers only to caller-supplied physical-label evidence; regions remain
  provisional even for a verified label. No exposure is silently selected or discarded.
  """

  high_pixel_count: int
  clipped_pixel_count: int
  label_verified: bool

  @property
  def label(self) -> str:
    """Caller-associated physical well label in A1..H12 order."""
    return super().label


@dataclass(frozen=True)
class RawCameraWellData:
  """Raw counts for one complete acquisition, retaining original bytes and provisional regions."""

  source: CameraAcquisition
  regions: DiagnosticPlateRegions

  def __post_init__(self) -> None:
    """Require complete, stationary observations and the explicitly associated camera/filter."""
    if self.source.state != "complete" or self.source.frame is None:
      raise ValueError("Raw well measurements require a complete acquisition")
    if self.source.geometry != CameraGeometry(321, 245):
      raise ValueError("Diagnostic regions require a 321 by 245 transfer")
    if self.source.camera_description != self.regions.camera_description:
      raise ValueError("Diagnostic regions belong to a different camera")
    if (
      self.source.optics_before != self.source.optics_after
      or not self.source.optics_before.is_stationary
      or self.source.optics_before.filter_position != self.regions.filter
    ):
      raise ValueError("Diagnostic regions do not match the unchanged stationary filter")

  @property
  def measurements(self) -> tuple[RawWellMeasurement, ...]:
    """Extract every specified pixel once from the rotated OPB=0 crop; retain clipped values."""
    frame = self.source.frame
    assert frame is not None
    pixels = struct.unpack("<76800H", frame.image_bytes)[::-1]
    if max(pixels) > 4095:
      raise ValueError("Raw diagnostic extraction requires the recorded 12-bit image format")
    results = []
    for well_index, region in enumerate(self.regions.wells, 1):
      values = tuple(pixels[row * 320 + column] for row, column in region)
      results.append(
        RawWellMeasurement(
          well_index,
          len(values),
          sum(values),
          min(values),
          max(values),
          sum(v > 4000 for v in values),
          sum(v == 4095 for v in values),
          well_index in self.regions.verified_wells,
        )
      )
    return tuple(results)


def _raw_table(
  collections: tuple[FluorescenceCollection, ...], regions: tuple[DiagnosticPlateRegions, ...]
) -> bytes:
  """Extract all planned exposures, preserving identity, hold association and quality flags."""
  by_filter = {region.filter: region for region in regions}
  expected_filters = {a.optics_before.filter_position for c in collections for a in c.acquisitions}
  if len(by_filter) != len(regions) or set(by_filter) != expected_filters:
    raise ValueError("Supply exactly one diagnostic region map for each acquired physical filter")
  region_hashes = {position: region.sha256 for position, region in by_filter.items()}
  output = io.StringIO(newline="")
  fields = (
    "run_id",
    "collection_id",
    "collection_ordinal",
    "stage",
    "cycle",
    "step_index",
    "physical_filter",
    "saved_filter",
    "acquisition_id",
    "exposure",
    "started_at",
    "finished_at",
    "sample_temperature_before",
    "sample_temperature_after",
    "well",
    "label",
    "pixel_count",
    "pixel_sum",
    "mean",
    "minimum",
    "maximum",
    "high_pixel_count",
    "clipped_pixel_count",
    "label_verified",
    "regions_provisional",
    "calibration_applied",
    "regions_sha256",
    "wire_sha256",
  )
  writer = csv.DictWriter(output, fieldnames=fields)
  writer.writeheader()
  for collection in collections:
    record = collection.record
    for acquisition in collection.acquisitions:
      after = acquisition.thermal_after
      if (
        after is None
        or not record.point.matches(acquisition.thermal_before)
        or not record.point.matches(after)
        or acquisition.thermal_before.run_start_record != record.before.run_start_record
        or after.run_start_record != record.before.run_start_record
        or not acquisition.optics_before.lamp_enabled
        or acquisition.optics_before.lamp_power != 1
      ):
        raise ValueError(
          "Raw run export requires matching collection hold, run and illuminated optics"
        )
      position = acquisition.optics_before.filter_position
      data = RawCameraWellData(acquisition, by_filter[position])
      wire_hash = hashlib.sha256(acquisition.wire_data).hexdigest()
      for measurement in data.measurements:
        writer.writerow(
          dict(
            zip(
              fields,
              (
                record.run_id,
                record.collection_id,
                record.ordinal,
                record.point.cycle_index + 1,
                record.point.repetition,
                record.point.step_index,
                position,
                6 - position,
                acquisition.acquisition_id,
                acquisition.exposure.duration,
                acquisition.started_at,
                acquisition.finished_at,
                acquisition.thermal_before.sample_temperature,
                after.sample_temperature,
                measurement.well,
                measurement.label,
                measurement.pixel_count,
                measurement.pixel_sum,
                measurement.mean,
                measurement.minimum,
                measurement.maximum,
                measurement.high_pixel_count,
                measurement.clipped_pixel_count,
                measurement.label_verified,
                True,
                False,
                region_hashes[position],
                wire_hash,
              ),
            )
          )
        )
  return output.getvalue().encode("utf-8")


_README = """ABI 7500 diagnostic raw measurements, format version 1

This offline export contains uncalibrated pixel counts from provisional caller-supplied regions.
It is not a vendor EDS or calibrated fluorescence result. No instrument I/O is performed.
Original transfers, acquisition and collection observations, thermal checkpoint and exact pixel
regions are included. The manifest hashes every other file. Region provenance records the input
association; it does not prove camera alignment, all physical labels or current calibration.

raw-measurements.csv contains 96 rows per exposure, with no exposure selection, averaging,
normalization, background subtraction, spectral unmixing, ROX ratio, Rn, delta-Rn or Ct.
stage is one-based cycle group, cycle is one-based repetition within it; step_index is zero-based.
collection_ordinal is one-based across the run. physical_filter is the wheel position 1..5;
saved_filter is 6 - physical_filter, NOT a dye measurement. exposure is commanded seconds.
Host timestamps do not establish sensor integration boundaries. Temperatures are degrees Celsius.
well is one-based A1..H12 order. Pixel count, sum, mean, minimum and maximum include zero pixels.
high_pixel_count counts pixels >4000; clipped_pixel_count counts pixels ==4095. The native
saturation selection rule is not applied to these differently sized diagnostic regions.
label_verified is caller-supplied independent label evidence, not validation of region boundaries.
regions_provisional is always True; calibration_applied is always False.
Boolean flags use True/False. Clipping counts cover selected pixels, not the entire physical well.
Use separate curves for each stage, step, physical filter, well and fixed exposure. Signals from
different exposures or filters cannot be pooled or treated as quantitative dye concentrations.

regions/*.json uses integer (row, column) pixels in the 240 by 320 OPB=0 crop rotated 180 degrees.
Its canonical JSON hash matches regions_sha256 in the table. Acquisition JSON replaces wire_data
with wire_data_member; restore that member's bytes.hex() before CameraAcquisition.from_dict().
wire_sha256 hashes the full original USB camera payload, including margins and trailer.
An export requires all planned exposures from a normally completed optical run. Missing, uncertain
or mismatched evidence fails rather than producing a complete-looking table. No file is overwritten.
Interrupted writes can leave a partial archive: require a readable ZIP and all manifest hashes.
"""


def write_raw_run_archive(
  path: Path,
  run: ThermalRun,
  collections: tuple[FluorescenceCollection, ...],
  regions: tuple[DiagnosticPlateRegions, ...],
) -> Path:
  """Export all raw well/exposure values and lossless inputs without accessing hardware.

  The run must be normally completed with every planned collection and exposure accounted for.
  Supply regions for this camera and every acquired physical filter. Validation and extraction
  finish before creating the file. Existing files are refused; caught write failures remove the
  newly created partial file. Keep full archives outside Git.
  """
  _validate_collections(run, collections)
  table = _raw_table(collections, regions)
  members: dict[str, dict[str, Any]] = {}

  def add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    """Index exact member bytes for independent verification and reprocessing."""
    archive.writestr(name, data)
    members[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

  output = path.open("xb")
  try:
    with output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
      add(archive, "README.txt", _README.encode("utf-8"))
      add(archive, "run.json", _json_bytes(run.to_dict()))
      add(archive, "raw-measurements.csv", table)
      for region in regions:
        add(archive, f"regions/physical-filter-{region.filter}.json", _json_bytes(region.to_dict()))
      for collection in collections:
        add(
          archive,
          f"collections/{collection.record.collection_id}.json",
          _json_bytes(collection.record.to_dict()),
        )
        for acquisition in collection.acquisitions:
          name = f"acquisitions/{acquisition.acquisition_id}"
          metadata = acquisition.to_dict()
          del metadata["wire_data"]
          metadata["wire_data_member"] = f"{name}.bin"
          add(archive, f"{name}.json", _json_bytes(metadata))
          add(archive, f"{name}.bin", acquisition.wire_data)
      archive.writestr(
        "manifest.json",
        _json_bytes(
          {
            "format": "pylabrobot.abi7500.raw-wells",
            "format_version": 1,
            "pylabrobot_version": __version__,
            "processing": "opb0-rotate180-diagnostic-regions-v1",
            "run_id": run.run_id,
            "collection_count": len(collections),
            "frame_count": sum(len(c.acquisitions) for c in collections),
            "measurement_count": sum(len(c.acquisitions) * 96 for c in collections),
            "regions_provisional": True,
            "calibration_applied": False,
            "files": members,
          }
        ),
      )
  except BaseException:
    path.unlink()
    raise
  return path
