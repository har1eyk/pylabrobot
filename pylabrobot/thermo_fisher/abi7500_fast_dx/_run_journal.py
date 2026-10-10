"""Durable run checkpoints and exclusive controller ownership using standard-library SQLite."""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .acquisition import CameraAcquisition
from .collection import CollectionRecord
from .run import ABI7500RunError, ThermalRun


class RunJournal:
  """One persistent journal per instrument, with a separate database providing a process lock.

  Checkpoints commit before actuation. The lock database stays in an exclusive transaction while
  the controller is using the journal; SQLite releases it if the process dies. Keeping the lock
  separate allows checkpoint commits without surrendering controller ownership. Use a local disk,
  not a network or synchronized folder. Opening another journal bypasses this ownership boundary.
  """

  def __init__(self, path: Path) -> None:
    """Remember a path without creating files or taking a lock."""
    self.path = path.expanduser().resolve()
    self._lock: Optional[sqlite3.Connection] = None
    self._db: Optional[sqlite3.Connection] = None

  def close(self) -> None:
    """Release journal connections and process ownership; retain all durable checkpoints."""
    if self._db is not None:
      self._db.close()
      self._db = None
    if self._lock is not None:
      self._lock.close()
      self._lock = None

  def _open(self) -> sqlite3.Connection:
    """Acquire exclusive ownership before reading or updating any run record."""
    if self._db is not None:
      return self._db
    self.path.parent.mkdir(parents=True, exist_ok=True)
    try:
      self._lock = sqlite3.connect(str(self.path) + ".lock", timeout=0)
      self._lock.execute("BEGIN EXCLUSIVE")
      self._db = sqlite3.connect(self.path, timeout=0)
      self._db.execute("PRAGMA synchronous=FULL")
      with self._db:
        self._db.execute(
          "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        self._db.execute(
          "CREATE TABLE IF NOT EXISTS acquisitions "
          "(acquisition_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS format (version INTEGER NOT NULL)")
        self._db.execute(
          "CREATE TABLE IF NOT EXISTS collections "
          "(collection_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        versions = self._db.execute("SELECT version FROM format").fetchall()
        if not versions:
          self._db.execute("INSERT INTO format VALUES (1)")
        elif versions != [(1,)]:
          raise ABI7500RunError("Unsupported thermal run journal format")
      return self._db
    except BaseException:
      self.close()
      raise

  def get(self, run_id: str) -> Optional[ThermalRun]:
    """Load a checkpoint, validating its primary key against its serialized identity."""
    row = self._open().execute("SELECT record FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
      return None
    run = ThermalRun.from_dict(json.loads(row[0]))
    if run.run_id != run_id:
      raise ABI7500RunError("Thermal run journal identity mismatch")
    return run

  def _check_owner(
    self, camera_description: str, thermal_version: str, permitted_run: Optional[str] = None
  ) -> None:
    """Refuse a different instrument or an unresolved thermal run before reserving an action."""
    db = self._open()
    for key, value in db.execute("SELECT run_id, record FROM runs"):
      prior = ThermalRun.from_dict(json.loads(value))
      if prior.run_id != key:
        raise ABI7500RunError("Thermal run journal identity mismatch")
      if (prior.camera_description, prior.thermal_version) != (camera_description, thermal_version):
        raise ABI7500RunError("Journal belongs to a different instrument or firmware")
      if not prior.is_terminal and prior.run_id != permitted_run:
        raise ABI7500RunError(f"Resolve run {prior.run_id!r} before another action")
    for key, value in db.execute("SELECT acquisition_id, record FROM acquisitions"):
      acquisition = CameraAcquisition.from_dict(json.loads(value))
      if acquisition.acquisition_id != key:
        raise ABI7500RunError("Camera acquisition journal identity mismatch")
      if (acquisition.camera_description, acquisition.thermal_version) != (
        camera_description,
        thermal_version,
      ):
        raise ABI7500RunError("Journal belongs to a different instrument or firmware")

  def check_optical_control(self, camera_description: str, thermal_version: str) -> None:
    """Hold process ownership and refuse optics changes during an unresolved thermal run."""
    self._check_owner(camera_description, thermal_version)

  def reserve(self, run: ThermalRun) -> None:
    """Persist a unique intent, refusing another unresolved run or a different instrument."""
    db = self._open()
    with db:
      self._check_owner(run.camera_description, run.thermal_version)
      db.execute("INSERT INTO runs VALUES (?, ?)", (run.run_id, json.dumps(run.to_dict())))

  def save(self, run: ThermalRun) -> ThermalRun:
    """Commit a checkpoint before a subsequent device action and return it unchanged."""
    db = self._open()
    with db:
      cursor = db.execute(
        "UPDATE runs SET record = ? WHERE run_id = ?", (json.dumps(run.to_dict()), run.run_id)
      )
      if cursor.rowcount != 1:
        raise ABI7500RunError("Cannot update an unreserved run")
    return run

  def get_acquisition(self, acquisition_id: str) -> Optional[CameraAcquisition]:
    """Load a snapshot without starting or resuming a hardware exposure."""
    row = (
      self._open()
      .execute("SELECT record FROM acquisitions WHERE acquisition_id = ?", (acquisition_id,))
      .fetchone()
    )
    if row is None:
      return None
    acquisition = CameraAcquisition.from_dict(json.loads(row[0]))
    if acquisition.acquisition_id != acquisition_id:
      raise ABI7500RunError("Camera acquisition journal identity mismatch")
    return acquisition

  def get_collection(self, collection_id: str) -> Optional[CollectionRecord]:
    """Read a collection intent, verifying its serialized primary key."""
    row = (
      self._open()
      .execute("SELECT record FROM collections WHERE collection_id = ?", (collection_id,))
      .fetchone()
    )
    if row is None:
      return None
    record = CollectionRecord.from_dict(json.loads(row[0]))
    if record.collection_id != collection_id:
      raise ABI7500RunError("Collection journal identity mismatch")
    return record

  def reserve_collection(self, record: CollectionRecord) -> None:
    """Persist a whole read before filter movement or any of its exposures can be attempted."""
    run = self.get(record.run_id)
    if run is None or run.collection_plan is None or run.state != "running" or run.collection_error:
      raise ABI7500RunError("Collection requires an owned active optical run")
    points = run.collection_plan.points(run.program)
    expected = tuple(
      f"{record.collection_id}-f{f.position}-e{index}"
      for f in run.collection_plan.filters
      for index in range(3)
    )
    if (
      record.point not in points
      or record.ordinal != points.index(record.point) + 1
      or record.before.run_start_record != run.start_record
      or record.acquisition_ids != expected
      or record.state != "uncertain"
    ):
      raise ABI7500RunError("Collection does not match its reserved thermal run and plan")
    db = self._open()
    with db:
      self._check_owner(run.camera_description, run.thermal_version, run.run_id)
      db.execute(
        "INSERT INTO collections VALUES (?, ?)",
        (record.collection_id, json.dumps(record.to_dict())),
      )

  def save_collection(self, record: CollectionRecord) -> CollectionRecord:
    """Checkpoint collection boundaries without changing its identity or permitting a retry."""
    db = self._open()
    with db:
      cursor = db.execute(
        "UPDATE collections SET record = ? WHERE collection_id = ?",
        (json.dumps(record.to_dict()), record.collection_id),
      )
      if cursor.rowcount != 1:
        raise ABI7500RunError("Cannot update an unreserved collection")
    return record

  def reserve_acquisition(
    self, acquisition: CameraAcquisition, *, collection_id: Optional[str] = None
  ) -> None:
    """Durably reserve an exposure ID before CJ or CC can reach hardware."""
    db = self._open()
    with db:
      permitted_run = None
      if collection_id is not None:
        record = self.get_collection(collection_id)
        run = None if record is None else self.get(record.run_id)
        if (
          record is None
          or record.state != "uncertain"
          or record.error is not None
          or acquisition.acquisition_id not in record.acquisition_ids
          or run is None
          or run.state != "running"
          or run.collection_plan is None
          or run.collection_error is not None
          or acquisition.thermal_before.run_start_record != run.start_record
          or not record.point.matches(acquisition.thermal_before)
        ):
          raise ABI7500RunError("Exposure is not part of an owned active collection")
        index = record.acquisition_ids.index(acquisition.acquisition_id)
        setting = run.collection_plan.filters[index // 3]
        if (
          acquisition.exposure.duration != setting.exposures[index % 3]
          or acquisition.optics_before.filter_position != setting.position
        ):
          raise ABI7500RunError("Exposure or filter disagrees with the durable collection plan")
        permitted_run = run.run_id
      self._check_owner(acquisition.camera_description, acquisition.thermal_version, permitted_run)
      db.execute(
        "INSERT INTO acquisitions VALUES (?, ?)",
        (acquisition.acquisition_id, json.dumps(acquisition.to_dict())),
      )

  def save_acquisition(self, acquisition: CameraAcquisition) -> CameraAcquisition:
    """Commit original data and metadata to the existing acquisition intent."""
    db = self._open()
    with db:
      cursor = db.execute(
        "UPDATE acquisitions SET record = ? WHERE acquisition_id = ?",
        (json.dumps(acquisition.to_dict()), acquisition.acquisition_id),
      )
      if cursor.rowcount != 1:
        raise ABI7500RunError("Cannot update an unreserved acquisition")
    return acquisition
