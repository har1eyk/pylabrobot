"""Portable completed-run evidence and measurement tables, without instrument I/O."""

import csv
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

from pylabrobot.__version__ import __version__

from .calibration import UniformityCalibration
from .collection import FluorescenceCollection
from .fluorescence import BackgroundCalibration
from .image_processing import PlatePixelMask
from .run import ABI7500RunError, ThermalRun

_KEYS = (
  "run_id",
  "collection_id",
  "collection_ordinal",
  "stage",
  "cycle",
  "step_index",
  "physical_filter",
  "saved_filter",
  "well",
  "label",
)
_MEASUREMENTS = _KEYS + (
  "acquisition_id",
  "selected_exposure_index",
  "exposure",
  "pre_uniformity_value",
  "uniformity_coefficient",
  "corrected_value",
  "corrected_float32_hex",
  "selected_saturated",
)
_EXPOSURES = _KEYS + (
  "acquisition_id",
  "exposure_index",
  "exposure",
  "started_at",
  "finished_at",
  "sample_temperature_before",
  "sample_temperature_after",
  "pixel_sum",
  "pixel_count",
  "ring_sum",
  "ring_count",
  "ring_subtracted_value",
  "saturation_count",
  "saturated",
  "background_offset",
  "background_slope",
  "pre_uniformity_value",
  "selected",
)
_README = """ABI 7500 Fast Dx fluorescence run archive, format version 1

This is an offline PLR evidence archive, not a vendor EDS file. No instrument commands are
sent by export. It contains a completed thermal checkpoint and every planned optical collection.
It does not establish current calibration validity, physical well orientation, dye concentration,
Rn, Ct, or assay accuracy. Calibration association is explicitly chosen by the caller.

manifest.json identifies the format, processing path, PLR version and supplied calibrations.
Its files map records each other member's byte count and SHA-256 (not an authenticity signature).
run.json preserves the thermal program, optical plan, instrument identity and completion evidence.
collections/<collection_id>.json preserves each collection checkpoint and closure observations.
acquisitions/<acquisition_id>.json preserves exposure, geometry, times and native observations.
The wire_data hex field is replaced by wire_data_member, naming the adjacent original .bin file;
restore wire_data from that file's bytes.hex() before CameraAcquisition.from_dict(). The binary
contains the entire USB camera payload, including borders and trailer, without cropping/rotation.
calibration/ contains the exact supplied background, uniformity and physical-filter mask bytes.

measurements.csv has one selected result per well/filter/collection; exposures.csv has all three
exposure candidates, including saturated and unselected ones. Rows follow collection, physical
filter and vendor well order (then exposure index). Fields common to both tables:
  run_id, collection_id: journal identities
  collection_ordinal: one-based optical point across the whole program
  stage: one-based cycle group; cycle: one-based repetition within that group
  step_index: zero-based step within the cycle group
  physical_filter: wheel position 1..5; saved_filter: vendor channel 6 - physical_filter
  well: vendor row-major index 1..96; label: A1..H12 (physical orientation unverified)
  acquisition_id: original frame identity; exposure: commanded duration in seconds
  pre_uniformity_value: background/exposure-corrected integer, NOT an uncorrected pixel sum

measurements.csv also contains:
  selected_exposure_index: zero-based candidate chosen by the native saturation rule
  uniformity_coefficient: binary32 factor from the supplied calibration
  corrected_value: binary32 uniformity-corrected filter signal in arbitrary units
  corrected_float32_hex: exact big-endian IEEE-754 bits, for lossless comparison
  selected_saturated: True when even the chosen fallback is flagged (never silently discarded)

exposures.csv also contains:
  exposure_index: zero-based index in the configured triplet
  started_at, finished_at: recorded host times, NOT measured sensor integration boundaries
  sample_temperature_before/after: instrument estimates in degrees Celsius
  pixel_sum/count, ring_sum/count: nonzero selected pixels and adjacent eight-neighbor ring
  ring_subtracted_value: max(0, trunc(pixel_sum - ring_sum/ring_count * pixel_count))
  saturation_count: selected pixels >4000; saturated: True when count >6
  background_offset/slope: supplied binary32 background coefficients for this well/channel
  selected: True when this candidate contributes the selected result

The supported processing path is OPB=0 crop, 180-degree rotation into mask coordinates, ring
subtraction, native background/exposure correction and exposure selection, then uniformity.
No drift normalization or spectral unmixing is performed. CSV numbers use round-trip decimal
representations, not display rounding; flags are True/False. Original inputs permit reprocessing.
An interrupted file write is not a completed export: require readable ZIP, manifest and all hashes.
Keep full archives outside Git. Export never overwrites an existing destination.
"""


def _json_bytes(value: Any) -> bytes:
  """Encode portable JSON, rejecting nonfinite numbers rather than emitting invalid JSON."""
  return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _validate_collections(run: ThermalRun, collections: tuple[FluorescenceCollection, ...]) -> None:
  """Reject partial, mismatched or ambiguous evidence before creating a completed-run archive."""
  if (
    run.state != "completed"
    or run.collection_error is not None
    or run.collection_plan is None
    or run.start_record is None
    or run.start_record in (bytes(9), run.baseline_start_record)
    or run.last_status is None
    or run.last_status.phase != "inactive"
    or run.last_status.state_code not in (1, 2)
    or run.last_status.run_start_record != run.start_record
    or run.history is None
    or run.history.outcome != "completed"
    or run.history.run_start_record != run.start_record
    or run.history.method_name != run.run_id
    or run.history.user_name != "PLR"
  ):
    raise ABI7500RunError("Export requires a completed optical run with matching thermal history")
  plan = run.collection_plan
  points = plan.points(run.program)
  if tuple(c.record.point for c in collections) != points:
    raise ABI7500RunError("Export requires every planned collection exactly once, in order")
  expected_exposures = tuple((f.position, e) for f in plan.filters for e in f.exposures)
  for ordinal, collection in enumerate(collections, 1):
    record = collection.record
    expected_ids = tuple(
      f"{record.collection_id}-f{f.position}-e{i}" for f in plan.filters for i in range(3)
    )
    if (
      record.run_id != run.run_id
      or record.ordinal != ordinal
      or record.state != "complete"
      or record.before.run_start_record != run.start_record
      or record.acquisition_ids != expected_ids
      or tuple(a.acquisition_id for a in collection.acquisitions) != expected_ids
      or record.homing_requested != (ordinal in plan.home_after_collections)
      or (plan.park_between_collections and record.parked_optics is None)
    ):
      raise ABI7500RunError("Export collection disagrees with its run and optical plan")
    for acquisition, (position, exposure) in zip(collection.acquisitions, expected_exposures):
      if (
        acquisition.camera_description != run.camera_description
        or acquisition.thermal_version != run.thermal_version
        or acquisition.exposure.duration != exposure
        or acquisition.optics_before.filter_position != position
      ):
        raise ABI7500RunError("Export frame identity, filter or exposure disagrees with its plan")


def _measurement_tables(
  collections: tuple[FluorescenceCollection, ...],
  background: BackgroundCalibration,
  uniformity: UniformityCalibration,
  masks: tuple[PlatePixelMask, ...],
) -> tuple[bytes, bytes]:
  """Compute each well once and preserve both selected values and all exposure candidates."""
  measurements, exposures = io.StringIO(newline=""), io.StringIO(newline="")
  selected_writer = csv.DictWriter(measurements, fieldnames=_MEASUREMENTS)
  exposure_writer = csv.DictWriter(exposures, fieldnames=_EXPOSURES)
  selected_writer.writeheader()
  exposure_writer.writeheader()
  offsets, slopes = background.offsets, background.slopes
  for collection in collections:
    results = collection.filter_results(background, uniformity, masks)
    for filter_index, result in enumerate(results):
      acquisitions = collection.acquisitions[3 * filter_index : 3 * filter_index + 3]
      for well in result.measurements:
        common = dict(
          zip(
            _KEYS,
            (
              collection.record.run_id,
              collection.record.collection_id,
              collection.record.ordinal,
              result.stage,
              result.cycle,
              collection.record.point.step_index,
              result.frames[0].mask.filter,
              result.filter,
              well.well,
              well.label,
            ),
          )
        )
        chosen = well.selection.selected_index
        selected_writer.writerow(
          {
            **common,
            "acquisition_id": acquisitions[chosen].acquisition_id,
            "selected_exposure_index": chosen,
            "exposure": well.selection.exposures[chosen],
            "pre_uniformity_value": well.raw_value,
            "uniformity_coefficient": well.uniformity_coefficient,
            "corrected_value": well.corrected_value,
            "corrected_float32_hex": struct.pack(">f", well.corrected_value).hex(),
            "selected_saturated": well.selected_saturated,
          }
        )
        for index, (acquisition, extraction) in enumerate(zip(acquisitions, well.extraction)):
          assert acquisition.thermal_after is not None  # Validated by filter_results().
          exposure_writer.writerow(
            {
              **common,
              "acquisition_id": acquisition.acquisition_id,
              "exposure_index": index,
              "exposure": well.selection.exposures[index],
              "started_at": acquisition.started_at,
              "finished_at": acquisition.finished_at,
              "sample_temperature_before": acquisition.thermal_before.sample_temperature,
              "sample_temperature_after": acquisition.thermal_after.sample_temperature,
              "pixel_sum": extraction.pixel_sum,
              "pixel_count": extraction.pixel_count,
              "ring_sum": extraction.ring_sum,
              "ring_count": extraction.ring_count,
              "ring_subtracted_value": extraction.raw_value,
              "saturation_count": extraction.saturation_count,
              "saturated": extraction.saturated,
              "background_offset": offsets[result.filter - 1][well.well - 1],
              "background_slope": slopes[result.filter - 1][well.well - 1],
              "pre_uniformity_value": well.selection.values[index],
              "selected": index == chosen,
            }
          )
  return measurements.getvalue().encode("utf-8"), exposures.getvalue().encode("utf-8")


def write_run_archive(
  path: Path,
  run: ThermalRun,
  collections: tuple[FluorescenceCollection, ...],
  background: BackgroundCalibration,
  uniformity: UniformityCalibration,
  masks: tuple[PlatePixelMask, ...],
) -> Path:
  """Write a new ZIP with lossless inputs, CSV outputs and content hashes; never access USB.

  Validation and processing finish before creating the destination. Existing files are rejected;
  a write failure removes the newly created partial file. A process/power failure can still leave
  a partial ZIP, which must not be treated as a completed export. The parent must already exist.
  """
  _validate_collections(run, collections)
  measurements, exposures = _measurement_tables(collections, background, uniformity, masks)
  members: dict[str, dict[str, Any]] = {}

  def add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    """Write a fixed relative member name and index its exact content bytes."""
    archive.writestr(name, data)
    members[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

  output = path.open("xb")  # Outside try: an existing file must never be removed on failure.
  try:
    with output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
      add(archive, "README.txt", _README.encode("utf-8"))
      add(archive, "run.json", _json_bytes(run.to_dict()))
      add(archive, "measurements.csv", measurements)
      add(archive, "exposures.csv", exposures)
      add(archive, "calibration/Background.csv", background.source_bytes)
      add(archive, "calibration/uniformity.ini", uniformity.source_bytes)
      mask_members = {}
      for mask in masks:
        name = f"calibration/physical-filter-{mask.filter}.aoi"
        mask_members[str(mask.filter)] = name
        add(archive, name, mask.source_bytes)
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
            "format": "pylabrobot.abi7500.fluorescence",
            "format_version": 1,
            "pylabrobot_version": __version__,
            "processing": "opb0-ring-background-exposure-uniformity-v1",
            "run_id": run.run_id,
            "run_member": "run.json",
            "collection_count": len(collections),
            "frame_count": sum(len(c.acquisitions) for c in collections),
            "measurement_count": sum(len(c.acquisitions) // 3 * 96 for c in collections),
            "calibration": {
              "background": "calibration/Background.csv",
              "uniformity": "calibration/uniformity.ini",
              "masks": mask_members,
            },
            "calibration_validity": "unverified",
            "physical_well_orientation": "unverified",
            "files": members,
          }
        ),
      )
  except BaseException:
    path.unlink()
    raise
  return path
