"""Durable run checkpoints and exclusive controller ownership using standard-library SQLite."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .acquisition import CameraAcquisition
from .calibration_exposure import CalibrationExposureProbe, CalibrationExposureSearch
from .collection import CollectionFilter, CollectionRecord
from .fluorescence import FluorescenceFrame
from .run import ABI7500RunError, ThermalRun


@dataclass(frozen=True)
class _RunOwner:
  """Validated identity and unresolved-action fields needed before another device action."""

  run_id: str
  camera_description: str
  thermal_version: str
  terminal: bool
  release_uncertain: bool


class RunJournal:
  """One persistent journal per instrument, with a separate database providing a process lock.

  Checkpoints commit before actuation. The lock database stays in an exclusive transaction while
  the controller is using the journal; SQLite releases it if the process dies. Keeping the lock
  separate allows checkpoint commits without surrendering controller ownership. Use a local disk,
  not a network or synchronized folder. Opening another journal bypasses this ownership boundary.
  Checkpoint writes wait up to five seconds for short read-only inspections; controller ownership
  remains fail-fast. Waiting for a database lock never repeats an instrument command.
  Ownership summaries are invalidated before local run writes and refreshed after any external
  database commit. Cached identities still undergo the permitted-run and unresolved-action checks.
  """

  def __init__(self, path: Path) -> None:
    """Remember a path without creating files or taking a lock."""
    self.path = path.expanduser().resolve()
    self._lock: Optional[sqlite3.Connection] = None
    self._db: Optional[sqlite3.Connection] = None
    self._acquisition_owner: Optional[tuple[str, str, int]] = None
    self._run_owners: Optional[tuple[int, tuple[_RunOwner, ...]]] = None

  def close(self) -> None:
    """Release journal connections and process ownership; retain all durable checkpoints."""
    self._acquisition_owner = None
    self._run_owners = None
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
      self._db = sqlite3.connect(self.path, timeout=5)
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
          self._db.execute("INSERT INTO format VALUES (2)")
        elif versions not in ([(1,)], [(2,)], [(3,)], [(4,)], [(5,)], [(6,)], [(7,)]):
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
    version = int(db.execute("PRAGMA data_version").fetchone()[0])
    if self._run_owners is None or self._run_owners[0] != version:
      owners = []
      for key, value in db.execute("SELECT run_id, record FROM runs"):
        prior = ThermalRun.from_dict(json.loads(value))
        if prior.run_id != key:
          raise ABI7500RunError("Thermal run journal identity mismatch")
        owners.append(
          _RunOwner(
            prior.run_id,
            prior.camera_description,
            prior.thermal_version,
            prior.is_terminal,
            prior.plate_release_state == "uncertain",
          )
        )
      self._run_owners = version, tuple(owners)
    for cached in self._run_owners[1]:
      if (cached.camera_description, cached.thermal_version) != (
        camera_description,
        thermal_version,
      ):
        raise ABI7500RunError("Journal belongs to a different instrument or firmware")
      if not cached.terminal and cached.run_id != permitted_run:
        raise ABI7500RunError(f"Resolve run {cached.run_id!r} before another action")
      if cached.release_uncertain and cached.run_id != permitted_run:
        raise ABI7500RunError(
          f"Resolve plate release for run {cached.run_id!r} before another action"
        )
    # This connection validates every acquisition it writes. Recheck historical images after
    # another connection commits, rather than decoding all previous frames before each exposure.
    owner = (camera_description, thermal_version, version)
    if self._acquisition_owner == owner:
      return
    self._acquisition_owner = None
    for key, value in db.execute("SELECT acquisition_id, record FROM acquisitions"):
      acquisition = CameraAcquisition.from_dict(json.loads(value))
      if acquisition.acquisition_id != key:
        raise ABI7500RunError("Camera acquisition journal identity mismatch")
      if (acquisition.camera_description, acquisition.thermal_version) != (
        camera_description,
        thermal_version,
      ):
        raise ABI7500RunError("Journal belongs to a different instrument or firmware")
    self._acquisition_owner = owner

  def check_optical_control(self, camera_description: str, thermal_version: str) -> None:
    """Hold process ownership and refuse optics changes during an unresolved thermal run."""
    self._check_owner(camera_description, thermal_version)

  def check_plate_release(self, run: ThermalRun) -> None:
    """Allow observation of this run's release while blocking other unresolved operations."""
    self._check_owner(run.camera_description, run.thermal_version, run.run_id)

  def reserve(self, run: ThermalRun) -> None:
    """Persist a unique intent, refusing another unresolved run or a different instrument."""
    db = self._open()
    with db:
      self._check_owner(run.camera_description, run.thermal_version)
      if run.cover_temperature is not None or (
        run.collection_plan is not None and run.collection_plan.exposure_search_masks
      ):
        # Older software must not ignore the cover intent or dynamic probe identities.
        db.execute("UPDATE format SET version = MAX(version, 3)")
      if run.start_command == 0x7E:
        db.execute("UPDATE format SET version = MAX(version, 4)")
      if run.calibration_startup or (
        run.collection_plan is not None and run.collection_plan.park_after_exposure_search
      ):
        db.execute("UPDATE format SET version = MAX(version, 5)")
      if run.standby_startup:
        # Older controllers must not ignore the journaled normal-start lamp transition.
        db.execute("UPDATE format SET version = MAX(version, 6)")
      if run.collection_plan is not None and run.collection_plan.cadence is not None:
        # Older controllers must not reinterpret burst observations as per-frame checks.
        db.execute("UPDATE format SET version = MAX(version, 7)")
      self._run_owners = None
      db.execute("INSERT INTO runs VALUES (?, ?)", (run.run_id, json.dumps(run.to_dict())))

  def save(self, run: ThermalRun) -> ThermalRun:
    """Commit a checkpoint before a subsequent device action and return it unchanged."""
    db = self._open()
    with db:
      self._run_owners = None
      if run.collection_plan is not None and run.collection_plan.cadence is not None:
        db.execute("UPDATE format SET version = MAX(version, 7)")
      if run.plate_release_state != "not_requested":
        # Commit the version with the intent so older controllers cannot ignore release state.
        db.execute("UPDATE format SET version = MAX(version, 2)")
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
      for index in range(len(f.exposures))
    )
    if (
      record.point not in points
      or record.ordinal != points.index(record.point) + 1
      or record.before.run_start_record != run.start_record
      or record.acquisition_ids != expected
      or record.homing_requested != (record.ordinal in run.collection_plan.home_after_collections)
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

  def calibration_search(
    self, record: CollectionRecord, setting: CollectionFilter
  ) -> CalibrationExposureSearch:
    """Replay complete saved probes; a missing prefix, partial image or wrong mask fails closed."""
    run = self.get(record.run_id)
    if run is None or run.collection_plan is None or setting not in run.collection_plan.filters:
      raise ABI7500RunError("Search is not associated with a saved collection plan")
    masks = {m.filter: m for m in run.collection_plan.exposure_search_masks}
    if setting.mode != "automatic" or setting.position not in masks:
      raise ABI7500RunError("No automatic exposure search for this filter")
    search = CalibrationExposureSearch()
    missing = False
    for index in range(11):
      acquisition = self.get_acquisition(f"{record.collection_id}-f{setting.position}-p{index}")
      if acquisition is None:
        missing = True
        continue
      if (
        missing
        or acquisition.state != "complete"
        or acquisition.frame is None
        or acquisition.camera_description != run.camera_description
        or acquisition.thermal_version != run.thermal_version
        or acquisition.optics_before.filter_position != setting.position
        or acquisition.optics_before != acquisition.optics_after
        or not record.point.matches(acquisition.thermal_before)
        or acquisition.thermal_after is None
        or not record.point.matches(acquisition.thermal_after)
        or acquisition.thermal_before.run_start_record != run.start_record
        or acquisition.thermal_after.run_start_record != run.start_record
      ):
        raise ABI7500RunError("Cannot replay an incomplete or inconsistent exposure probe")
      search = search.add(
        CalibrationExposureProbe.from_frame(
          FluorescenceFrame(acquisition.frame, acquisition.exposure, masks[setting.position])
        )
      )
    return search

  def _reserve_acquisition(
    self, acquisition: CameraAcquisition, *, collection_id: Optional[str] = None
  ) -> None:
    """Validate and insert an intent inside the caller's checkpoint transaction."""
    db = self._open()
    permitted_run = None
    if collection_id is not None:
      record = self.get_collection(collection_id)
      run = None if record is None else self.get(record.run_id)
      if (
        record is None
        or record.state != "uncertain"
        or record.error is not None
        or run is None
        or run.state != "running"
        or run.collection_plan is None
        or run.collection_error is not None
        or acquisition.thermal_before.run_start_record != run.start_record
        or not record.point.matches(acquisition.thermal_before)
      ):
        raise ABI7500RunError("Exposure is not part of an owned active collection")
      settings = {
        f"{collection_id}-f{setting.position}-e{index}": (setting, exposure)
        for setting in run.collection_plan.filters
        for index, exposure in enumerate(setting.exposures)
      }
      probes = {
        f"{collection_id}-f{setting.position}-p{index}": setting
        for setting in run.collection_plan.filters
        if setting.mode == "automatic"
        for index in range(11)
      }
      key = acquisition.acquisition_id
      if key in probes:
        setting = probes[key]
        search = self.calibration_search(record, setting)
        if (
          key != f"{collection_id}-f{setting.position}-p{len(search.probes)}"
          or search.next_milliseconds is None
        ):
          raise ABI7500RunError("Probe is not the next reserved search step")
        exposure = search.next_milliseconds / 1000
      elif key in settings and key in record.acquisition_ids:
        setting, exposure = settings[key]
        if setting.mode == "automatic":
          selected = self.calibration_search(record, setting).selected_milliseconds
          if selected is None:
            raise ABI7500RunError("Final images require a complete exposure search")
          exposure = selected / 1000
      else:
        raise ABI7500RunError("Exposure is not part of the reserved collection")
      if (
        acquisition.exposure.duration != exposure
        or acquisition.optics_before.filter_position != setting.position
        or acquisition.observation_scope
        != ("frame" if run.collection_plan.cadence is None else "filter_burst")
      ):
        raise ABI7500RunError("Exposure or filter disagrees with the durable collection plan")
      permitted_run = run.run_id
    elif acquisition.observation_scope != "frame":
      raise ABI7500RunError("Burst observations require a reserved collection")
    self._check_owner(acquisition.camera_description, acquisition.thermal_version, permitted_run)
    db.execute(
      "INSERT INTO acquisitions VALUES (?, ?)",
      (acquisition.acquisition_id, json.dumps(acquisition.to_dict())),
    )

  def reserve_acquisition(
    self, acquisition: CameraAcquisition, *, collection_id: Optional[str] = None
  ) -> None:
    """Durably reserve an exposure ID before CJ or CC can reach hardware."""
    with self._open():
      self._reserve_acquisition(acquisition, collection_id=collection_id)

  def _save_acquisition(self, acquisition: CameraAcquisition) -> None:
    """Write an existing intent inside the caller's checkpoint transaction."""
    if self._acquisition_owner is not None and self._acquisition_owner[:2] != (
      acquisition.camera_description,
      acquisition.thermal_version,
    ):
      self._acquisition_owner = None
    cursor = self._open().execute(
      "UPDATE acquisitions SET record = ? WHERE acquisition_id = ?",
      (json.dumps(acquisition.to_dict()), acquisition.acquisition_id),
    )
    if cursor.rowcount != 1:
      raise ABI7500RunError("Cannot update an unreserved acquisition")

  def save_acquisitions(self, acquisitions: tuple[CameraAcquisition, ...]) -> None:
    """Atomically commit at most one filter burst after its shared boundary checks.

    No member is visible as complete unless the whole transaction commits. A failed write or
    commit rolls back every update; previously committed original frames remain available.
    """
    if not 1 <= len(acquisitions) <= 16 or len({a.acquisition_id for a in acquisitions}) != len(
      acquisitions
    ):
      raise ValueError("Checkpoint requires 1–16 distinct acquisitions")
    with self._open():
      for acquisition in acquisitions:
        self._save_acquisition(acquisition)

  def save_acquisition(self, acquisition: CameraAcquisition) -> CameraAcquisition:
    """Commit original data and metadata to the existing acquisition intent."""
    self.save_acquisitions((acquisition,))
    return acquisition

  def save_acquisition_and_reserve_next(
    self,
    previous: CameraAcquisition,
    following: CameraAcquisition,
    *,
    collection_id: str,
  ) -> None:
    """Commit original bytes and the next same-filter intent before any subsequent USB command.

    Both writes use one FULL synchronous transaction. Reservation still validates the current
    run, collection, filter, exposure and ownership; duplicate intents are never replaced.
    Failure rolls back both writes. The caller must retain and save the downloaded frame on
    failure, without sending CJ or CC for the rejected next intent.
    """
    with self._open():
      record = self.get_collection(collection_id)
      if (
        record is None
        or previous.acquisition_id not in record.acquisition_ids
        or following.acquisition_id not in record.acquisition_ids
        or record.acquisition_ids.index(following.acquisition_id)
        != record.acquisition_ids.index(previous.acquisition_id) + 1
        or previous.observation_scope != "filter_burst"
        or following.observation_scope != "filter_burst"
        or previous.state != "uncertain"
        or previous.frame is None
        or previous.exposure_start_offset is None
        or previous.camera_description != following.camera_description
        or previous.thermal_version != following.thermal_version
        or previous.thermal_before != following.thermal_before
        or previous.optics_before != following.optics_before
      ):
        raise ABI7500RunError("Combined checkpoint requires adjacent images in the same burst")
      self._reserve_acquisition(following, collection_id=collection_id)
      self._save_acquisition(previous)
