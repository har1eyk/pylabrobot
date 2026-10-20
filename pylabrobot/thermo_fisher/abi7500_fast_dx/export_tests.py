"""Check portable exports against captured numerical oracles without opening hardware."""

import csv
import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pylabrobot.io.usb import USB

from ._export import write_run_archive
from ._run_journal import RunJournal
from .acquisition import CameraAcquisition, CameraExposure, OpticalState
from .collection import CollectionFilter, CollectionPlan, CollectionRecord, FluorescenceCollection
from .collection_tests import _status
from .driver import ABI7500FastDx
from .fluorescence_tests import _VECTORS, _background, _frames, _uniformity
from .image_processing_tests import _acquisition
from .run import ABI7500History, ABI7500RunError, ThermalRun
from .run_tests import _history, _running


def _saved_run(vector: dict[str, Any]) -> tuple[ThermalRun, tuple[FluorescenceCollection, ...]]:
  """Associate recorded image patches with explicitly synthetic completed-run metadata."""
  frames = _frames(vector)
  position = vector["physical_filter"]
  plan = CollectionPlan(
    ((1, 1),),
    (CollectionFilter(position, tuple(vector["exposures"])),),
    park_between_collections=True,
    home_after_collections=(2,),
  )
  run = replace(_running("acquire"), collection_plan=plan)
  assert run.start_record is not None
  history = bytearray(_history("acquire", run.run_id))
  history[6:8] = b"\0\0"
  history[10:19] = run.start_record
  run = replace(
    run,
    state="completed",
    history=ABI7500History(bytes(history)),
    last_status=_status(2, remaining=0, state=2, phase=0),
  )
  template = _acquisition()
  optics = OpticalState(position.to_bytes(2, "big"), b"\0\0", b"\0\1\0\1")
  parked = replace(optics, filter_response=b"\0\0")
  collections = []
  for ordinal, point in enumerate(plan.points(run.program), 1):
    status = _status(point.repetition)
    acquisitions = tuple(
      replace(
        template,
        acquisition_id=f"{point.collection_id(run.run_id)}-f{position}-e{index}",
        camera_description=run.camera_description,
        thermal_version=run.thermal_version,
        exposure=frame.exposure,
        exposure_reply=frame.exposure.milliseconds.to_bytes(2, "little"),
        wire_data=frame.source.wire_data,
        thermal_before=status,
        thermal_after=status,
        optics_before=optics,
        optics_after=optics,
      )
      for index, frame in enumerate(frames)
    )
    record = CollectionRecord(
      run.run_id,
      point,
      ordinal,
      status,
      tuple(a.acquisition_id for a in acquisitions),
      "complete",
      status,
      parked_optics=parked,
      homing_requested=ordinal == 2,
    )
    collections.append(FluorescenceCollection(record, acquisitions))
  return run, tuple(collections)


class ExportTests(unittest.TestCase):
  """Lossless inputs, correct row identities/precision and rejection of incomplete evidence."""

  def setUp(self) -> None:
    """Create one synthetic run from an unsaturated recorded patch."""
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.path = Path(self.directory.name) / "results.zip"
    self.vector = _VECTORS[0]
    self.checkpoint, self.collections = _saved_run(self.vector)
    self.background = _background(self.vector["background_offset"], self.vector["background_slope"])
    self.uniformity = _uniformity(self.vector["uniformity_factor"])
    self.masks = (_frames(self.vector)[0].mask,)

  def export(self) -> Path:
    """Write the test's explicitly associated immutable inputs."""
    return write_run_archive(
      self.path, self.checkpoint, self.collections, self.background, self.uniformity, self.masks
    )

  def test_inputs_roundtrip_and_every_member_hashed(self) -> None:
    """Reconstruct every acquisition and checkpoint, preserving all wire and calibration bytes."""
    self.assertEqual(self.export(), self.path)
    with zipfile.ZipFile(self.path) as archive:
      self.assertIsNone(archive.testzip())
      manifest = json.loads(archive.read("manifest.json"))
      self.assertEqual(set(manifest["files"]), set(archive.namelist()) - {"manifest.json"})
      self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
      self.assertEqual(manifest["format_version"], 1)
      self.assertEqual(manifest["calibration_validity"], "unverified")
      self.assertEqual(manifest["physical_well_orientation"], "unverified")
      self.assertEqual(manifest["frame_count"], 6)
      self.assertEqual(manifest["measurement_count"], 192)
      for name, evidence in manifest["files"].items():
        data = archive.read(name)
        self.assertEqual(evidence, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
      self.assertEqual(ThermalRun.from_dict(json.loads(archive.read("run.json"))), self.checkpoint)
      self.assertEqual(
        archive.read(manifest["calibration"]["background"]), self.background.source_bytes
      )
      self.assertEqual(
        archive.read(manifest["calibration"]["uniformity"]), self.uniformity.source_bytes
      )
      self.assertEqual(
        archive.read(manifest["calibration"]["masks"]["1"]), self.masks[0].source_bytes
      )
      for collection in self.collections:
        record = collection.record
        self.assertEqual(
          CollectionRecord.from_dict(
            json.loads(archive.read(f"collections/{record.collection_id}.json"))
          ),
          record,
        )
        for acquisition in collection.acquisitions:
          metadata = json.loads(archive.read(f"acquisitions/{acquisition.acquisition_id}.json"))
          metadata["wire_data"] = archive.read(metadata.pop("wire_data_member")).hex()
          self.assertEqual(CameraAcquisition.from_dict(metadata), acquisition)

  def test_csv_matches_native_oracles_and_preserves_saturated_fallback(self) -> None:
    """All filter mappings, exact float bits and selected/unselected flags survive CSV export."""
    for vector in _VECTORS:
      with self.subTest(filter=vector["physical_filter"], cycle=vector["cycle"]):
        run, collections = _saved_run(vector)
        background = _background(vector["background_offset"], vector["background_slope"])
        uniformity = _uniformity(vector["uniformity_factor"])
        path = self.path.with_name(f"{vector['physical_filter']}-{vector['cycle']}.zip")
        write_run_archive(
          path, run, collections, background, uniformity, (_frames(vector)[0].mask,)
        )
        with zipfile.ZipFile(path) as archive:
          rows = list(csv.DictReader(io.StringIO(archive.read("measurements.csv").decode())))
          exposures = list(csv.DictReader(io.StringIO(archive.read("exposures.csv").decode())))
        self.assertEqual(len(rows), 192)
        self.assertEqual(len(exposures), 576)
        for index, row in enumerate(rows):
          well = index % 96 + 1
          cycle = index // 96 + 1
          self.assertEqual(row["label"], f"{chr(65 + (well - 1) // 12)}{(well - 1) % 12 + 1}")
          self.assertEqual(int(row["well"]), well)
          self.assertEqual((row["stage"], row["cycle"], row["step_index"]), ("2", str(cycle), "1"))
          self.assertEqual(row["collection_ordinal"], str(cycle))
          self.assertEqual(int(row["physical_filter"]), vector["physical_filter"])
          self.assertEqual(int(row["saved_filter"]), vector["saved_filter"])
          self.assertEqual(int(row["pre_uniformity_value"]), vector["raw_value"])
          self.assertEqual(row["corrected_float32_hex"], vector["corrected_bits"])
          self.assertEqual(
            struct.pack(">f", float(row["corrected_value"])).hex(), vector["corrected_bits"]
          )
          chosen = vector["selected_index"]
          self.assertEqual(int(row["selected_exposure_index"]), chosen)
          self.assertEqual(row["selected_saturated"], str(vector["saturated"][chosen]))
          candidates = exposures[3 * index : 3 * index + 3]
          self.assertEqual(
            [c["saturated"] for c in candidates], list(map(str, vector["saturated"]))
          )
          self.assertEqual(
            [c["selected"] for c in candidates], [str(i == chosen) for i in range(3)]
          )
          self.assertEqual(candidates[chosen]["pre_uniformity_value"], row["pre_uniformity_value"])
          self.assertEqual(candidates[chosen]["acquisition_id"], row["acquisition_id"])
          self.assertEqual([float(c["exposure"]) for c in candidates], vector["exposures"])

  def test_reject_incomplete_thermal_or_optical_outcomes(self) -> None:
    """Thermal completion alone cannot turn cancelled, partial or ambiguous optics into success."""
    for run in (
      replace(self.checkpoint, state="cancelled"),
      replace(self.checkpoint, state="uncertain"),
      replace(self.checkpoint, collection_error="missed window"),
      replace(self.checkpoint, history=None),
      replace(self.checkpoint, last_status=_status()),
      replace(self.checkpoint, last_status=None),
      replace(self.checkpoint, start_record=b"x" * 9),
      replace(self.checkpoint, collection_plan=None),
    ):
      with self.subTest(run=run.state), self.assertRaises(ABI7500RunError):
        write_run_archive(
          self.path, run, self.collections, self.background, self.uniformity, self.masks
        )
      self.assertFalse(self.path.exists())
    first, second = self.collections
    for collections in (
      (),
      (first,),
      (second, first),
      (first, first),
      (replace(first, record=replace(first.record, state="uncertain", error="lost reply")), second),
      (replace(first, acquisitions=first.acquisitions[:-1]), second),
      (replace(first, record=replace(first.record, parked_optics=None)), second),
      (first, replace(second, record=replace(second.record, homing_requested=False))),
    ):
      with self.subTest(count=len(collections)), self.assertRaises(ABI7500RunError):
        write_run_archive(
          self.path, self.checkpoint, collections, self.background, self.uniformity, self.masks
        )
      self.assertFalse(self.path.exists())

  def test_reject_mismatched_frames_and_calibration_masks(self) -> None:
    """Mixed devices, exposures, holds, frame states and mask assignments must not be exported."""
    first, second = self.collections
    frame = first.acquisitions[0]
    for invalid in (
      replace(frame, camera_description="different camera"),
      replace(frame, thermal_version="different firmware"),
      replace(frame, exposure=CameraExposure(0.010), exposure_reply=b"\x0a\x00"),
      replace(frame, thermal_after=_status(2)),
      replace(frame, state="uncertain", error="interrupted transfer"),
    ):
      collections = (replace(first, acquisitions=(invalid, *first.acquisitions[1:])), second)
      with (
        self.subTest(frame=invalid.acquisition_id),
        self.assertRaises((ABI7500RunError, ValueError)),
      ):
        write_run_archive(
          self.path, self.checkpoint, collections, self.background, self.uniformity, self.masks
        )
      self.assertFalse(self.path.exists())
    for masks in ((), self.masks * 2, (replace(self.masks[0], filter=2),)):
      with self.subTest(masks=len(masks)), self.assertRaises(ValueError):
        write_run_archive(
          self.path, self.checkpoint, self.collections, self.background, self.uniformity, masks
        )
      self.assertFalse(self.path.exists())

  def test_existing_file_is_never_overwritten(self) -> None:
    """Exclusive creation preserves an unrelated file, including on a repeat export attempt."""
    self.path.write_bytes(b"keep this existing file")
    with self.assertRaises(FileExistsError):
      self.export()
    self.assertEqual(self.path.read_bytes(), b"keep this existing file")

  def test_fixed_calibration_collection_cannot_use_bracket_export(self) -> None:
    """Calibration averages must not be interpreted as three-exposure measurements."""
    plan = self.checkpoint.collection_plan
    assert plan is not None
    fixed = replace(plan.filters[0], mode="fixed", exposures=(0.720,))
    run = replace(self.checkpoint, collection_plan=replace(plan, filters=(fixed,)))
    with self.assertRaisesRegex(ABI7500RunError, "requires brackets"):
      write_run_archive(
        self.path, run, self.collections, self.background, self.uniformity, self.masks
      )
    self.assertFalse(self.path.exists())

  def test_write_failure_removes_only_new_partial_archive(self) -> None:
    """A failed write cannot leave a misleading success archive or damage another file."""
    other = self.path.with_name("keep.zip")
    other.write_bytes(b"unrelated")
    with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
      with self.assertRaisesRegex(OSError, "disk full"):
        self.export()
    self.assertFalse(self.path.exists())
    self.assertEqual(other.read_bytes(), b"unrelated")


class OfflineExportTests(unittest.IsolatedAsyncioTestCase):
  """The public journal API exports without setup, reconciliation or USB access."""

  async def test_export_after_reopening_journal_without_setup(self) -> None:
    """A saved completed run is sufficient; any transport operation would fail this test."""
    run, collections = _saved_run(_VECTORS[0])
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory)
      journal = RunJournal(path / "journal.sqlite3")
      journal.reserve(replace(run, state="running"))
      for collection in collections:
        record = collection.record
        journal.reserve_collection(replace(record, state="uncertain"))
        for acquisition in collection.acquisitions:
          journal.reserve_acquisition(acquisition, collection_id=record.collection_id)
        journal.save_collection(record)
      journal.save(run)
      journal.close()
      before = (path / "journal.sqlite3").read_bytes()
      with (
        patch.object(USB, "setup", side_effect=AssertionError("No USB setup")),
        patch.object(USB, "read", side_effect=AssertionError("No USB reads")),
        patch.object(USB, "write", side_effect=AssertionError("No USB writes")),
      ):
        instrument = ABI7500FastDx(run_journal=path / "journal.sqlite3")
        try:
          output = await instrument.export_run(
            run.run_id,
            path / "export.zip",
            background=_background(0, 0),
            uniformity=_uniformity(1),
            masks=(_frames(_VECTORS[0])[0].mask,),
          )
          self.assertTrue(output.is_file())
          with self.assertRaisesRegex(ABI7500RunError, "Unknown"):
            await instrument.export_run(
              "unknown",
              path / "unknown.zip",
              background=_background(0, 0),
              uniformity=_uniformity(1),
              masks=(_frames(_VECTORS[0])[0].mask,),
            )
          self.assertFalse((path / "unknown.zip").exists())
        finally:
          await instrument.stop()
      self.assertEqual((path / "journal.sqlite3").read_bytes(), before)
