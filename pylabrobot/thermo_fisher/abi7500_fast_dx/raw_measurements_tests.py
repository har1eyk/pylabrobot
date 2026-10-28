"""Raw-region measurements and complete-run exports never open the instrument."""

import base64
import csv
import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from pylabrobot.io.usb import USB

from .acquisition import CameraAcquisition, CameraExposure
from .camera import CameraGeometry
from .collection import CollectionFilter
from .collection_tests import _status
from .export_tests import _saved_run
from .fluorescence import FluorescenceFrame
from .fluorescence_tests import _VECTORS
from .image_processing_tests import _acquisition
from .raw_measurements import DiagnosticPlateRegions, RawCameraWellData, write_raw_run_archive
from .run import ABI7500RunError, ThermalRun
from .status import ABI7500Status


def _regions(camera: str, filter: int = 1) -> DiagnosticPlateRegions:
  """Use asymmetric pixel regions with independently identifiable first and last wells."""
  wells = (((10, 10), (10, 11), (11, 10), (11, 12)),) + tuple(
    ((20 + i // 12, 20 + i % 12),) for i in range(95)
  )
  return DiagnosticPlateRegions(camera, filter, wells, "Synthetic regions", (1,), "Synthetic label")


def _wire(values: dict[tuple[int, int], int]) -> bytes:
  """Put explicit display pixels in the opposite wire corner; leave unused margins distinctive."""
  pixels = [60000] * (321 * 245)
  for row in range(240):
    for column in range(320):
      pixels[(244 - row) * 321 - 1 + (319 - column)] = values.get((row, column), 100)
  return struct.pack("<78645H", *pixels) + bytes(range(22))


class RawMeasurementTests(unittest.TestCase):
  """Enforce image coordinates, exact membership, raw arithmetic and explicit provenance."""

  def setUp(self) -> None:
    """Retain captured observations but replace pixels with an asymmetric synthetic pattern."""
    self.source = replace(
      _acquisition(),
      wire_data=_wire(
        {
          (10, 10): 0,
          (10, 11): 4000,
          (11, 10): 4001,
          (11, 12): 4095,
          (27, 30): 1,
        }
      ),
    )
    self.regions = _regions(self.source.camera_description)

  def test_zero_inclusion_orientation_and_clipping(self) -> None:
    """Exact sum includes zero; the >4000 threshold differs from 4095 clipping."""
    data = RawCameraWellData(self.source, self.regions)
    values = data.measurements
    self.assertIs(data.source, self.source)
    self.assertIs(data.regions, self.regions)
    first = values[0]
    self.assertEqual((first.label, first.pixel_count, first.pixel_sum), ("A1", 4, 12096))
    self.assertEqual((first.minimum, first.maximum, first.mean), (0, 4095, 3024.0))
    self.assertEqual((first.high_pixel_count, first.clipped_pixel_count), (2, 1))
    self.assertTrue(first.label_verified)
    self.assertFalse(values[1].label_verified)
    self.assertEqual((values[-1].label, values[-1].mean), ("H12", 1.0))
    self.assertEqual([v.label for v in values][::12], [f"{row}1" for row in "ABCDEFGH"])

  def test_circle_edges_and_fractional_centers(self) -> None:
    """Boundary pixels are included; a fractional center changes exact membership."""
    centers = tuple((10.0 + i // 12 * 25, 10.0 + i % 12 * 25) for i in range(96))
    arguments: dict[str, Any] = dict(
      camera_description="camera", filter=5, provenance="independent grid"
    )
    regions = DiagnosticPlateRegions.from_circles(centers, 1, **arguments)
    self.assertEqual(regions.wells[0], ((9, 10), (10, 9), (10, 10), (10, 11), (11, 10)))
    shifted = DiagnosticPlateRegions.from_circles(((10.5, 10.5),) + centers[1:], 1, **arguments)
    self.assertEqual(shifted.wells[0], ((10, 10), (10, 11), (11, 10), (11, 11)))
    for radius in (0, -1, float("nan"), float("inf"), True, 20):
      with self.subTest(radius=radius), self.assertRaises(ValueError):
        DiagnosticPlateRegions.from_circles(centers, radius, **arguments)
    for centers_bad in (
      centers[:-1],
      ((0.0, 0.0),) + centers[1:],
      ((float("nan"), 10.0),) + centers[1:],
      (centers[1],) + centers[1:],
    ):
      with self.subTest(centers=centers_bad[:1]), self.assertRaises(ValueError):
        DiagnosticPlateRegions.from_circles(centers_bad, 1, **arguments)

  def test_region_schema_and_association(self) -> None:
    """Region serialization retains all evidence and cannot claim nonprovisional coordinates."""
    decoded = DiagnosticPlateRegions.from_dict(json.loads(json.dumps(self.regions.to_dict())))
    self.assertEqual(decoded, self.regions)
    self.assertEqual(decoded.sha256, self.regions.sha256)
    self.assertNotEqual(replace(decoded, filter=2).sha256, decoded.sha256)
    self.assertNotEqual(replace(decoded, label_evidence="different").sha256, decoded.sha256)
    for key, value in (
      ("format_version", True),
      ("format_version", 2),
      ("coordinates", "opb1"),
      ("regions_provisional", False),
    ):
      with self.subTest(key=key), self.assertRaises(ValueError):
        DiagnosticPlateRegions.from_dict(dict(decoded.to_dict(), **{key: value}))
    invalid = decoded.to_dict()
    invalid["wells"][0][0].append(9)
    with self.assertRaises(ValueError):
      DiagnosticPlateRegions.from_dict(invalid)

  def test_reject_ambiguous_regions_and_label_claims(self) -> None:
    """Empty, repeated, shared, mutable and out-of-bounds pixels cannot silently bias means."""
    for first in (
      (),
      ((-1, 1),),
      ((240, 1),),
      ((1, 320),),
      ((True, 1),),
      ((10, 10), (10, 10)),
      self.regions.wells[1],
      ((1.2, 2),),
    ):
      with self.subTest(first=first), self.assertRaises(ValueError):
        replace(self.regions, wells=(cast(Any, first),) + self.regions.wells[1:])
    for invalid in (
      dict(wells=self.regions.wells[:-1]),
      dict(wells=list(self.regions.wells)),
      dict(filter=0),
      dict(filter=6),
      dict(filter=True),
      dict(provenance=""),
      dict(camera_description=""),
      dict(verified_wells=(0,)),
      dict(verified_wells=(1, 1)),
      dict(verified_wells=(97,)),
      dict(verified_wells=(True,)),
      dict(label_evidence=""),
    ):
      with self.subTest(invalid=invalid), self.assertRaises(ValueError):
        replace(self.regions, **invalid)

  def test_reject_incompatible_or_uncertain_acquisition(self) -> None:
    """A full payload alone cannot justify extraction when camera or observations disagree."""
    source = self.source
    for changed in (
      replace(source, state="uncertain"),
      replace(source, camera_description="different camera"),
      replace(source, optics_after=replace(source.optics_before, filter_response=b"\0\2")),
      replace(
        source,
        optics_before=replace(source.optics_before, filter_response=b"\0\2"),
        optics_after=replace(source.optics_before, filter_response=b"\0\2"),
      ),
      replace(source, geometry=CameraGeometry(2, 6), wire_data=bytes(128)),
    ):
      with self.subTest(source=changed.acquisition_id), self.assertRaises(ValueError):
        RawCameraWellData(changed, self.regions)
    with self.assertRaisesRegex(ValueError, "12-bit"):
      RawCameraWellData(
        replace(source, wire_data=_wire({(200, 200): 4096})), self.regions
      ).measurements

  def test_recorded_markers_and_blanks_against_independent_analysis(self) -> None:
    """Three saved exposures match the pre-existing analysis of six markers and two blanks."""
    fixture = json.loads(
      (Path(__file__).parent / "test_data/diagnostic_region_vectors.json").read_text()
    )
    wells = [((0, index),) for index in range(96)]  # Synthetic, unmeasured placeholders.
    for region in fixture["regions"]:
      wells[region["well"] - 1] = tuple(tuple(pixel) for pixel in region["pixels"])
    regions = DiagnosticPlateRegions(
      fixture["camera_description"], 5, tuple(wells), fixture["description"]
    )
    optics = replace(self.source.optics_before, filter_response=b"\0\5")
    count = sum(len(region["pixels"]) for region in fixture["regions"])
    for frame in fixture["frames"]:
      values = iter(
        struct.unpack(
          f"<{count}H",
          zlib.decompress(base64.b64decode(frame["selected_pixels_u16le_zlib_base64"])),
        )
      )
      selected = {
        tuple(pixel): next(values) for region in fixture["regions"] for pixel in region["pixels"]
      }
      exposure = CameraExposure(frame["exposure"])
      source = replace(
        self.source,
        camera_description=regions.camera_description,
        acquisition_id=frame["acquisition_id"],
        exposure=exposure,
        exposure_reply=exposure.milliseconds.to_bytes(2, "little"),
        optics_before=optics,
        optics_after=optics,
        wire_data=_wire(selected),
      )
      measurements = RawCameraWellData(source, regions).measurements
      for expected in frame["expected"]:
        actual = measurements[expected["well"] - 1]
        self.assertEqual(
          (
            actual.pixel_count,
            actual.mean,
            actual.minimum,
            actual.maximum,
            actual.clipped_pixel_count,
          ),
          (
            expected["pixel_count"],
            expected["mean"],
            expected["minimum"],
            expected["maximum"],
            expected["clipped_pixel_count"],
          ),
        )

  def test_diagnostic_regions_cannot_be_used_as_pixel_masks(self) -> None:
    """A matching pixel-interface shape must not bypass the calibrated pipeline's mask boundary."""
    frame = self.source.frame
    assert frame is not None
    with self.assertRaisesRegex(TypeError, "explicit PlatePixelMask"):
      FluorescenceFrame(frame, self.source.exposure, cast(Any, self.regions))


class RawExportTests(unittest.TestCase):
  """Raw archives preserve all exposures, identities and originals without fabricated correction."""

  def setUp(self) -> None:
    """Use a synthetic two-cycle completed optical run with captured controller observations."""
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.path = Path(self.directory.name) / "raw.zip"
    self.checkpoint, collections = _saved_run(_VECTORS[0])
    self.collections = tuple(
      replace(
        c,
        acquisitions=tuple(
          replace(a, wire_data=_wire({(10, 10): 4095 if i == 0 else 50}))
          for i, a in enumerate(c.acquisitions)
        ),
      )
      for c in collections
    )
    self.regions: tuple[DiagnosticPlateRegions, ...] = (
      _regions(self.checkpoint.camera_description),
    )

  def export(self) -> Path:
    """Write an archive while forbidding USB construction, reads and writes."""
    with (
      patch.object(USB, "__init__", side_effect=AssertionError("USB constructed")),
      patch.object(USB, "write", side_effect=AssertionError("USB written")),
      patch.object(USB, "read", side_effect=AssertionError("USB read")),
    ):
      return write_raw_run_archive(self.path, self.checkpoint, self.collections, self.regions)

  def test_lossless_export_and_fixed_exposure_rows(self) -> None:
    """Retain 96 rows for every exposure, including clipped data, with exact input hashes."""
    self.assertEqual(self.export(), self.path)
    with zipfile.ZipFile(self.path) as archive:
      self.assertIsNone(archive.testzip())
      manifest = json.loads(archive.read("manifest.json"))
      self.assertEqual(manifest["format"], "pylabrobot.abi7500.raw-wells")
      self.assertEqual((manifest["frame_count"], manifest["measurement_count"]), (6, 576))
      self.assertTrue(manifest["regions_provisional"])
      self.assertFalse(manifest["calibration_applied"])
      self.assertEqual(set(manifest["files"]), set(archive.namelist()) - {"manifest.json"})
      self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
      for name, evidence in manifest["files"].items():
        data = archive.read(name)
        self.assertEqual(evidence, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
      self.assertEqual(ThermalRun.from_dict(json.loads(archive.read("run.json"))), self.checkpoint)
      region_bytes = archive.read("regions/physical-filter-1.json")
      self.assertEqual(hashlib.sha256(region_bytes).hexdigest(), self.regions[0].sha256)
      self.assertEqual(DiagnosticPlateRegions.from_dict(json.loads(region_bytes)), self.regions[0])
      rows = list(csv.DictReader(io.StringIO(archive.read("raw-measurements.csv").decode())))
      self.assertEqual(len(rows), 576)
      self.assertTrue(all(r["regions_provisional"] == "True" for r in rows))
      self.assertTrue(all(r["calibration_applied"] == "False" for r in rows))
      self.assertEqual({r["cycle"] for r in rows}, {"1", "2"})
      self.assertEqual({r["saved_filter"] for r in rows}, {"5"})
      self.assertFalse({"corrected_value", "Rn", "Ct", "rox_ratio"} & set(rows[0]))
      for collection in self.collections:
        for acquisition in collection.acquisitions:
          selected = [r for r in rows if r["acquisition_id"] == acquisition.acquisition_id]
          self.assertEqual(len(selected), 96)
          self.assertEqual(
            {float(r["exposure"]) for r in selected}, {acquisition.exposure.duration}
          )
          self.assertEqual(
            {r["wire_sha256"] for r in selected},
            {hashlib.sha256(acquisition.wire_data).hexdigest()},
          )
          first = selected[0]
          if acquisition.acquisition_id.endswith("e0"):
            self.assertEqual((first["pixel_sum"], first["clipped_pixel_count"]), ("4395", "1"))
          else:
            self.assertEqual((first["pixel_sum"], first["clipped_pixel_count"]), ("350", "0"))
          metadata = json.loads(archive.read(f"acquisitions/{acquisition.acquisition_id}.json"))
          metadata["wire_data"] = archive.read(metadata.pop("wire_data_member")).hex()
          self.assertEqual(CameraAcquisition.from_dict(metadata), acquisition)

  def test_missing_duplicate_or_wrong_order_collection(self) -> None:
    """An apparently complete thermal run cannot hide missing or duplicated optical data."""
    original = self.collections
    for invalid in ((), original[:1], original[::-1], (original[0], original[0])):
      self.collections = invalid
      with self.assertRaises(ABI7500RunError):
        self.export()
      self.assertFalse(self.path.exists())

  def test_two_physical_filters_remain_separate(self) -> None:
    """A second filter produces its own rows and region hash, never a fabricated dye ratio."""
    plan = self.checkpoint.collection_plan
    assert plan is not None
    self.checkpoint = replace(
      self.checkpoint,
      collection_plan=replace(
        plan, filters=plan.filters + (CollectionFilter(2, plan.filters[0].exposures),)
      ),
    )
    collections = []
    for collection in self.collections:
      second = tuple(
        replace(
          a,
          acquisition_id=f"{collection.record.collection_id}-f2-e{i}",
          optics_before=replace(a.optics_before, filter_response=b"\0\2"),
          optics_after=replace(a.optics_before, filter_response=b"\0\2"),
          wire_data=_wire({(10, 10): 200}),
        )
        for i, a in enumerate(collection.acquisitions)
      )
      frames = collection.acquisitions + second
      collections.append(
        replace(
          collection,
          acquisitions=frames,
          record=replace(
            collection.record, acquisition_ids=tuple(a.acquisition_id for a in frames)
          ),
        )
      )
    self.collections = tuple(collections)
    self.regions = self.regions + (replace(self.regions[0], filter=2),)
    self.export()
    with zipfile.ZipFile(self.path) as archive:
      rows = list(csv.DictReader(io.StringIO(archive.read("raw-measurements.csv").decode())))
    self.assertEqual(len(rows), 1152)
    for position, saved in ((1, "5"), (2, "4")):
      selected = [r for r in rows if r["physical_filter"] == str(position)]
      self.assertEqual(len(selected), 576)
      self.assertEqual({r["saved_filter"] for r in selected}, {saved})
      self.assertEqual({r["regions_sha256"] for r in selected}, {self.regions[position - 1].sha256})
    self.assertEqual(
      {r["pixel_sum"] for r in rows if r["physical_filter"] == "2" and r["label"] == "A1"}, {"500"}
    )

  def test_wrong_frame_metadata_or_hold(self) -> None:
    """Reject camera, exposure, run-token, timing and uncertain-frame mismatches before writing."""
    collection = self.collections[0]
    original = collection.acquisitions[0]
    mismatch = bytearray(original.thermal_before.raw_payload)
    mismatch[32:41] = bytes(range(9))
    other_run = ABI7500Status(bytes(mismatch))
    for invalid in (
      replace(original, camera_description="wrong camera"),
      replace(original, thermal_version="wrong firmware"),
      replace(original, state="uncertain"),
      replace(original, thermal_before=other_run, thermal_after=other_run),
      replace(original, thermal_after=_status(2)),
      replace(original, thermal_after=_status(1, remaining=0)),
      replace(original, exposure=CameraExposure(0.005), exposure_reply=b"\x05\0"),
      replace(original, optics_after=replace(original.optics_before, filter_response=b"\0\2")),
    ):
      self.collections = (
        replace(collection, acquisitions=(invalid,) + collection.acquisitions[1:]),
        self.collections[1],
      )
      with self.assertRaises((ValueError, ABI7500RunError)):
        self.export()
      self.assertFalse(self.path.exists())

  def test_missing_duplicate_or_uncertain_exposure(self) -> None:
    """The complete collection flag cannot conceal an absent, repeated or reordered exposure."""
    collection = self.collections[0]
    frames = collection.acquisitions
    for changed in (
      replace(collection, acquisitions=frames[:2]),
      replace(collection, acquisitions=frames[::-1]),
      replace(collection, acquisitions=(frames[0], frames[0], frames[2])),
      replace(collection, record=replace(collection.record, state="uncertain")),
    ):
      self.collections = (changed, self.collections[1])
      with self.assertRaises(ABI7500RunError):
        self.export()
      self.assertFalse(self.path.exists())

  def test_missing_or_duplicate_map_and_different_camera(self) -> None:
    """No silent fallback to another filter or instrument's provisional regions."""
    region = self.regions[0]
    for regions in (
      (),
      (region, region),
      (replace(region, filter=2),),
      (replace(region, camera_description="other camera"),),
    ):
      self.regions = regions
      with self.assertRaises(ValueError):
        self.export()
      self.assertFalse(self.path.exists())

  def test_failed_run_and_existing_file(self) -> None:
    """Failure is explicit and does not destroy an existing artifact."""
    self.checkpoint = replace(self.checkpoint, collection_error="lost exposure")
    with self.assertRaises(ABI7500RunError):
      self.export()
    self.assertFalse(self.path.exists())
    self.checkpoint = replace(self.checkpoint, collection_error=None)
    self.path.write_bytes(b"keep this")
    with self.assertRaises(FileExistsError):
      self.export()
    self.assertEqual(self.path.read_bytes(), b"keep this")

  def test_write_failure_removes_partial_archive(self) -> None:
    """An exception while writing cannot leave a successful-looking archive."""
    with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
      with self.assertRaisesRegex(OSError, "disk full"):
        self.export()
    self.assertFalse(self.path.exists())
