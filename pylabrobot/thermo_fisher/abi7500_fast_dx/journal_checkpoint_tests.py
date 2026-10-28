"""Verify atomic burst checkpoints, rollback and unchanged ownership checks without USB."""

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ._run_journal import RunJournal
from .acquisition import CameraAcquisition, CameraExposure, OpticalState
from .acquisition_tests import _IMAGE
from .cadence_tests import _plan, _status
from .camera import CameraGeometry
from .collection import CollectionPoint, CollectionRecord
from .collection_tests import _run
from .run import ABI7500RunError


def _exit_during_checkpoint(
  path: Path,
  previous: CameraAcquisition,
  following: CameraAcquisition,
  collection_id: str,
  before_commit: bool,
) -> None:
  """Exit without cleanup after dirty-page spill or after the checkpoint returns."""
  journal = RunJournal(path)
  db = journal._open()
  db.execute("PRAGMA cache_size=1")

  def observe(statement: str) -> None:
    """Interrupt COMMIT before SQLite executes it, preserving the recovery journal."""
    if before_commit and statement == "COMMIT":
      os._exit(91)

  db.set_trace_callback(observe)
  journal.save_acquisition_and_reserve_next(previous, following, collection_id=collection_id)
  os._exit(92)


class BurstCheckpointTests(unittest.TestCase):
  """Read through independent connections to distinguish committed records from pending writes."""

  def setUp(self) -> None:
    """Reserve a synthetic running collection and one empty first intent."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.journal = RunJournal(self.path)
    original = _run()
    program = replace(
      original.program,
      cycles=tuple(
        replace(c, steps=tuple(replace(step, hold_time=312) for step in c.steps))
        for c in original.program.cycles
      ),
    )
    self.thermal_run = replace(original, program=program, collection_plan=_plan())
    self.journal.reserve(self.thermal_run)
    point = CollectionPoint(1, 1, 1)
    ids = tuple(f"{point.collection_id(self.thermal_run.run_id)}-f3-e{i}" for i in range(16))
    self.record = CollectionRecord(self.thermal_run.run_id, point, 1, _status(), ids)
    self.journal.reserve_collection(self.record)
    self.first = CameraAcquisition(
      ids[0],
      CameraExposure(0.594),
      self.thermal_run.camera_description,
      self.thermal_run.thermal_version,
      CameraGeometry(321, 245),
      "2026-10-26T00:00:00+00:00",
      _status(),
      OpticalState(b"\0\3", b"\0\0", b"\0\1\0\1"),
      observation_scope="filter_burst",
    )
    self.next = replace(self.first, acquisition_id=ids[1])
    self.raw = replace(
      self.first,
      wire_data=_IMAGE,
      exposure_reply=b"\x52\x02",
      readiness_replies=(b"ready",),
      exposure_start_offset=0.0,
    )
    self.journal.reserve_acquisition(self.first, collection_id=self.record.collection_id)

  def tearDown(self) -> None:
    """Remove only the temporary journal after releasing ownership."""
    self.journal.close()
    self.temp.cleanup()

  def read_committed(self, acquisition_id: str) -> Optional[CameraAcquisition]:
    """Observe disk state using a separate read-only connection."""
    with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
      row = db.execute(
        "SELECT record FROM acquisitions WHERE acquisition_id=?", (acquisition_id,)
      ).fetchone()
    return None if row is None else CameraAcquisition.from_dict(json.loads(row[0]))

  def checkpoint(self) -> None:
    """Save the downloaded first frame and reserve its immediate successor."""
    self.journal.save_acquisition_and_reserve_next(
      self.raw, self.next, collection_id=self.record.collection_id
    )

  def crash_checkpoint(self, before_commit: bool) -> None:
    """Kill only an isolated worker and recover through the public journal methods."""
    self.journal.close()
    worker = multiprocessing.get_context("spawn").Process(
      target=_exit_during_checkpoint,
      args=(self.path, self.raw, self.next, self.record.collection_id, before_commit),
    )
    worker.start()
    try:
      worker.join(timeout=15)
      self.assertEqual(worker.exitcode, 91 if before_commit else 92)
    finally:
      if worker.is_alive():
        worker.kill()
        worker.join(timeout=5)
      worker.close()
    self.assertEqual(self.journal._open().execute("PRAGMA quick_check").fetchone(), ("ok",))

  def test_process_exit_before_commit_rolls_back_both_records(self) -> None:
    """An interrupted checkpoint cannot expose its new intent or partial frame update."""
    self.crash_checkpoint(before_commit=True)
    self.assertEqual(self.journal.get_acquisition(self.first.acquisition_id), self.first)
    self.assertIsNone(self.journal.get_acquisition(self.next.acquisition_id))
    self.checkpoint()
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.raw)

  def test_process_exit_after_commit_preserves_bytes_and_blocks_duplicate(self) -> None:
    """Committed bytes and intent survive abrupt process exit without allowing another exposure."""
    self.crash_checkpoint(before_commit=False)
    self.assertEqual(self.journal.get_acquisition(self.first.acquisition_id), self.raw)
    self.assertEqual(self.journal.get_acquisition(self.next.acquisition_id), self.next)
    with self.assertRaises(sqlite3.IntegrityError):
      self.journal.reserve_acquisition(self.next, collection_id=self.record.collection_id)

  def test_both_records_committed_before_return_in_one_full_transaction(self) -> None:
    """A single durable commit makes original bytes and the next intent visible together."""
    statements: list[str] = []
    self.journal._open().set_trace_callback(statements.append)
    self.checkpoint()
    self.assertEqual(sum(sql == "COMMIT" for sql in statements), 1)
    self.assertEqual(self.journal._open().execute("PRAGMA synchronous").fetchone(), (2,))
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.raw)
    self.assertEqual(self.read_committed(self.next.acquisition_id), self.next)
    self.journal.close()
    reopened = RunJournal(self.path)
    try:
      self.assertEqual(reopened.get_acquisition(self.first.acquisition_id), self.raw)
      with self.assertRaises(sqlite3.IntegrityError):
        reopened.reserve_acquisition(self.next, collection_id=self.record.collection_id)
    finally:
      reopened.close()

  def test_write_failure_rolls_back_new_intent_and_previous_update(self) -> None:
    """A failed image write cannot leave a misleading reserved successor."""
    db = self.journal._open()
    db.execute(
      "CREATE TEMP TRIGGER reject_frame BEFORE UPDATE ON acquisitions "
      "BEGIN SELECT RAISE(ABORT, 'disk write rejected'); END"
    )
    with self.assertRaisesRegex(sqlite3.IntegrityError, "disk write rejected"):
      self.checkpoint()
    self.assertFalse(db.in_transaction)
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.first)
    self.assertIsNone(self.read_committed(self.next.acquisition_id))
    db.execute("DROP TRIGGER reject_frame")
    self.journal.save_acquisition(self.raw)
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.raw)

  def test_commit_failure_rolls_back_both_records(self) -> None:
    """An external reader blocking COMMIT must not expose either half of a checkpoint."""
    db = self.journal._open()
    db.execute("PRAGMA busy_timeout=1")
    reader = sqlite3.connect(self.path)
    try:
      reader.execute("BEGIN")
      reader.execute("SELECT record FROM acquisitions").fetchall()
      with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
        self.checkpoint()
      self.assertFalse(db.in_transaction)
    finally:
      reader.close()
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.first)
    self.assertIsNone(self.read_committed(self.next.acquisition_id))

  def test_duplicate_reservation_preserves_both_existing_records(self) -> None:
    """A repeated intent remains an error and never replaces an earlier frame or exposure."""
    self.journal.reserve_acquisition(self.next, collection_id=self.record.collection_id)
    with self.assertRaises(sqlite3.IntegrityError):
      self.checkpoint()
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.first)
    self.assertEqual(self.read_committed(self.next.acquisition_id), self.next)

  def test_external_run_change_still_blocks_combined_checkpoint(self) -> None:
    """Fewer commits must not cache away a run's cancellation or collection failure."""
    with sqlite3.connect(self.path) as external:
      external.execute(
        "UPDATE runs SET record=? WHERE run_id=?",
        (
          json.dumps(replace(self.thermal_run, state="cancelled").to_dict()),
          self.thermal_run.run_id,
        ),
      )
    with self.assertRaisesRegex(ABI7500RunError, "owned active collection"):
      self.checkpoint()
    self.assertIsNone(self.read_committed(self.next.acquisition_id))

  def test_wrong_exposure_still_fails_plan_validation(self) -> None:
    """The successor's exposure is checked against the durable plan before committing."""
    self.next = replace(self.next, exposure=CameraExposure(0.5))
    with self.assertRaisesRegex(ABI7500RunError, "durable collection plan"):
      self.checkpoint()
    self.assertIsNone(self.read_committed(self.next.acquisition_id))

  def test_partial_or_nonadjacent_frame_is_not_a_combined_checkpoint(self) -> None:
    """Only complete raw transfers and immediate same-burst successors may share a commit."""
    for previous, following in (
      (replace(self.raw, wire_data=_IMAGE[:50]), self.next),
      (self.raw, replace(self.next, acquisition_id=self.record.acquisition_ids[2])),
      (self.raw, replace(self.next, optics_before=OpticalState(b"\0\4", b"\0\0", b"\0\1\0\1"))),
    ):
      with self.subTest(previous=previous.acquisition_id, following=following.acquisition_id):
        with self.assertRaisesRegex(ABI7500RunError, "adjacent images"):
          self.journal.save_acquisition_and_reserve_next(
            previous, following, collection_id=self.record.collection_id
          )
    self.assertEqual(self.read_committed(self.first.acquisition_id), self.first)

  def test_final_batch_failure_keeps_every_frame_uncertain(self) -> None:
    """An update error after the first row cannot mark only part of a burst complete."""
    self.checkpoint()
    second_raw = replace(
      self.raw, acquisition_id=self.next.acquisition_id, exposure_start_offset=1.0
    )
    self.journal.save_acquisition(second_raw)
    completed = tuple(
      replace(
        a,
        state="complete",
        finished_at=a.started_at,
        thermal_after=a.thermal_before,
        optics_after=a.optics_before,
      )
      for a in (self.raw, second_raw)
    )
    db = self.journal._open()
    db.execute(
      "CREATE TEMP TRIGGER reject_second BEFORE UPDATE ON acquisitions "
      "WHEN NEW.acquisition_id LIKE '%-e1' "
      "BEGIN SELECT RAISE(ABORT, 'final update rejected'); END"
    )
    with self.assertRaisesRegex(sqlite3.IntegrityError, "final update rejected"):
      self.journal.save_acquisitions(completed)
    self.assertEqual(self.read_committed(self.raw.acquisition_id), self.raw)
    self.assertEqual(self.read_committed(second_raw.acquisition_id), second_raw)
    db.execute("DROP TRIGGER reject_second")
    self.journal.save_acquisitions(completed)
    self.assertEqual(self.read_committed(self.raw.acquisition_id), completed[0])
    self.assertEqual(self.read_committed(second_raw.acquisition_id), completed[1])
