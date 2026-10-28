"""Verify captured calibration cadence, boundary observations and durable failure handling."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Optional, cast
from unittest.mock import patch

from pylabrobot.io.usb import USB

from ._run_journal import RunJournal
from .acquisition import CameraAcquisition, CameraExposure
from .acquisition_tests import _IMAGE
from .camera import CameraGeometry
from .collection import CollectionCadence, CollectionFilter, CollectionPlan, CollectionPoint
from .collection_tests import _CollectionUSB, _run
from .collection_tests import _status as _base_status
from .driver import ABI7500FastDx
from .protocol import ABI7500ProtocolError, ThermalPacket
from .run import ABI7500RunError
from .run_tests import _RUN_ID
from .status import ABI7500Status

_EVIDENCE = json.loads((Path(__file__).parent / "test_data/hex_cadence_vectors.json").read_text())
_BOUNDARY = json.loads((Path(__file__).parent / "test_data/hex_boundary_vectors.json").read_text())
_IMAGE_TIMING = json.loads(
  (Path(__file__).parent / "test_data/hex_image_timing_vectors.json").read_text()
)
_REAL_SLEEP = asyncio.sleep
_POINT = CollectionPoint(1, 1, 1)


def _status(repetition: int = 1, remaining: int = 45) -> ABI7500Status:
  """Use a synthetic 90-degree cover around the captured ownership and hold fields."""
  payload = bytearray(_base_status(repetition, remaining).raw_payload)
  payload[8:12] = (900).to_bytes(4, "big")
  return ABI7500Status(bytes(payload))


def _plan(full: bool = False) -> CollectionPlan:
  """Use original final HEX exposures and CC offsets, optionally just one 16-image burst."""
  groups = _EVIDENCE["filters"] if full else [_EVIDENCE["filters"][2]]
  origin = groups[0]["start_offsets"][0]
  return CollectionPlan(
    ((1, 1),),
    tuple(
      CollectionFilter(
        g["physical_filter"], (g["exposure_ms"] / 1000,) * len(g["start_offsets"]), "fixed"
      )
      for g in groups
    ),
    read_window=205,
    cadence=CollectionCadence(tuple(tuple(t - origin for t in g["start_offsets"]) for g in groups)),
  )


class _Clock:
  """Advance monotonic test time without sleeping through a thermal program."""

  def __init__(self) -> None:
    """Use a nonzero origin to catch absolute-versus-relative deadline errors."""
    self.now = 100.0

  def time(self) -> float:
    """Return synthetic elapsed time."""
    return self.now

  async def sleep(self, delay: float) -> None:
    """Advance the requested wait and yield to cancellation tasks."""
    self.now += max(0, delay)
    await _REAL_SLEEP(0)


class _CadenceUSB(_CollectionUSB):
  """Strict bytes with explicit synthetic exposure, transfer and controller latencies."""

  def __init__(self, clock: _Clock) -> None:
    """Retain CC timestamps and support one delayed or truncated image."""
    super().__init__()
    self.clock = clock
    self.starts: list[float] = []
    self.duration = 0.0
    self.image_bytes = 0
    self.reading_image = False
    self.delay_image: Optional[int] = None
    self.partial_image: Optional[int] = None

  def image(self) -> None:
    """Exercise an odd fragment followed by bounded calibration image reads."""
    self.expect_read(_IMAGE[:7], 16384)
    offset = 7
    while offset < len(_IMAGE):
      size = min(16384, len(_IMAGE) - offset)
      self.expect_read(_IMAGE[offset : offset + size], size)
      offset += size

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Account for controller latency; no thermal commands may appear inside a burst script."""
    await super().write(data, timeout)
    if data.startswith(b"RS"):
      self.clock.now += 0.11 if self.last_command == 0x29 else 0.06
    elif data.startswith(b"CJ"):
      self.duration = int(data.split(b",")[1]) / 1000
      self.clock.now += 0.001
    elif data == b"CC":
      self.starts.append(self.clock.now)
    elif data.startswith(b"CD"):
      self.image_bytes = 0
      self.reading_image = True

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Charge image latency by fragment size, with faults after retained partial data."""
    if self.reading_image and self.partial_image == len(self.starts) and self.image_bytes > 4096:
      raise TimeoutError("Truncated scheduled image")
    result = await super().read(size, timeout)
    if result == b"ready" and self.starts:
      self.clock.now = max(self.clock.now, self.starts[-1] + self.duration + 0.052)
    if self.reading_image:
      self.image_bytes += len(result)
      self.clock.now += 0.327 * len(result) / len(_IMAGE)
      if self.image_bytes == len(_IMAGE):
        self.reading_image = False
        if self.delay_image == len(self.starts):
          self.clock.now += 1.0
    return result

  def burst_point(
    self, plan: CollectionPlan, *, changed: str = "", initial_position: int = 0
  ) -> None:
    """Script complete filter groups with observations only at their boundaries."""
    assert plan.cadence is not None
    self.observe(_status(remaining=205))
    self.optics(initial_position)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")
    for filter_index, (setting, offsets) in enumerate(zip(plan.filters, plan.cadence.offsets)):
      before = _status(remaining=205 - int(offsets[0]))
      if filter_index == 0:
        self.exchange(0x29, before.raw_payload)
      if filter_index > 0 or initial_position != setting.position:
        self.exchange(0x69, request=setting.position.to_bytes(2, "big"))
        for _ in range(9):
          self.exchange(0x6C, b"\0\1")
        self.exchange(0x6C, b"\0\2" if changed == "motor" else b"\0\0")
        self.exchange(0x6B, (0 if changed == "position" else setting.position).to_bytes(2, "big"))
        self.exchange(0x66, b"\0\1\0\0" if changed == "before-lamp" else b"\0\1\0\1")
      else:
        self.optics(setting.position)
      if changed == "short-hold":
        before = _status(remaining=2)
      if filter_index == 1:
        if changed == "before-hold":
          before = _status(repetition=2)
        if changed in ("before-owner", "before-cover"):
          payload = bytearray(before.raw_payload)
          if changed == "before-owner":
            payload[39] ^= 1
          else:
            payload[8:12] = (800).to_bytes(4, "big")
          before = ABI7500Status(bytes(payload))
      self.exchange(0x29, before.raw_payload)
      for duration in setting.exposures:
        exposure = CameraExposure(duration)
        self.expect_write(exposure.encode())
        self.expect_read(
          (0 if changed == "exposure" else exposure.milliseconds).to_bytes(2, "little")
        )
        self.expect_write(b"CC")
        self.expect_write(b"CH   5")
        self.expect_read(b"ready")
        self.expect_write(CameraGeometry(321, 245).encode_read())
        self.image()
      self.optics(setting.position, power=0 if changed == "lamp" else 1)
      after = _status(remaining=205 - int(offsets[-1] + setting.exposures[-1] + 1))
      if changed == "hold":
        after = _status(repetition=2)
      if changed == "owner":
        payload = bytearray(after.raw_payload)
        payload[39] ^= 1
        after = type(after)(bytes(payload))
      if changed == "cover":
        payload = bytearray(after.raw_payload)
        payload[8:12] = (800).to_bytes(4, "big")
        after = type(after)(bytes(payload))
      self.exchange(0x29, after.raw_payload)
    self.exchange(0x29, _status(remaining=80).raw_payload)


class _RecordedBoundaryUSB(_CadenceUSB):
  """Replay measured controller and local-work costs around the first filter transition."""

  def __init__(self, clock: _Clock) -> None:
    """Keep measured delays separate from synthetic timing outside this captured boundary."""
    super().__init__(clock)
    self.boundary_cursor = 0
    self.skipped_queries = 0

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Charge recorded command costs, allowing only the two redundant queries to be omitted."""
    await super().write(data, timeout)
    if len(self.starts) != 1:
      return
    if data.startswith(b"RS"):
      entries = _BOUNDARY["commands"]
      while int(entries[self.boundary_cursor]["command"], 16) != self.last_command:
        assert entries[self.boundary_cursor]["redundant"]
        self.boundary_cursor += 1
        self.skipped_queries += 1
      entry = entries[self.boundary_cursor]
      assert ThermalPacket.decode(data[6:]).payload.hex() == entry["payload"]
      self.boundary_cursor += 1
      self.clock.now += entry["elapsed_to_next_command_without_poll_sleep"] - (
        0.11 if self.last_command == 0x29 else 0.06
      )
    elif data.startswith(b"CJ"):
      assert self.boundary_cursor == len(_BOUNDARY["commands"])
      self.clock.now += _BOUNDARY["exposure_configuration_duration"] - 0.001

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Include observed image transfer and local persistence gaps in the virtual clock."""
    before, reading = self.clock.now, self.reading_image
    result = await super().read(size, timeout)
    if len(self.starts) == 1:
      if result == b"ready":
        self.clock.now = max(before, self.starts[0] + _BOUNDARY["first_download_start"])
      if reading:
        self.clock.now = before + _BOUNDARY["download_duration"] * len(result) / len(_IMAGE)
        if not self.reading_image:
          self.clock.now += _BOUNDARY["after_image_gap"]
    return result


class _RecordedImageUSB(_CadenceUSB):
  """Replay all measured intra-filter time without assuming which local cost was saved."""

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Align configuration replies to recorded offsets, including the blocked sixth one."""
    await super().write(data, timeout)
    if data.startswith(b"CJ") and self.starts:
      self.clock.now = (
        self.starts[0] + _IMAGE_TIMING["images"][len(self.starts) - 1]["next_CJ_reply_offset"]
      )

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Charge the exact recorded readiness/download durations for the first five images."""
    before, reading = self.clock.now, self.reading_image
    result = await super().read(size, timeout)
    if self.starts:
      measured = _IMAGE_TIMING["images"][len(self.starts) - 1]
      if result == b"ready":
        self.clock.now = self.starts[-1] + measured["CC_to_CD_seconds"]
      if reading:
        self.clock.now = before + measured["download_seconds"] * len(result) / len(_IMAGE)
    return result


class CadencePlanTests(unittest.TestCase):
  """Captured offsets are opt-in, bounded and cannot silently alter existing protocols."""

  def test_captured_65_frame_plan_and_legacy_roundtrip(self) -> None:
    """All final CC intervals survive plan serialization without modifying legacy plans."""
    plan = _plan(True)
    self.assertEqual(CollectionPlan.from_dict(plan.to_dict()), plan)
    assert plan.cadence is not None
    self.assertEqual(sum(map(len, plan.cadence.offsets)), 65)
    self.assertAlmostEqual(plan.cadence.offsets[-1][-1], 107.708686)
    legacy = replace(plan, cadence=None)
    self.assertNotIn("cadence", legacy.to_dict())
    self.assertIsNone(CollectionPlan.from_dict(legacy.to_dict()).cadence)

  def test_invalid_cadence_is_rejected_before_io(self) -> None:
    """Reject nonfinite, reordered, overlapping, undersized and non-fixed schedules."""
    for offsets in (
      (),
      ((1.0,),),
      ((0.0,), (0.0,)),
      ((0.0,), (float("nan"),)),
      ((False,),),
      ([0.0],),
      ((0.0, 1.0),),
    ):
      with self.subTest(offsets=offsets), self.assertRaises((ValueError, TypeError)):
        CollectionCadence(offsets)  # type: ignore[arg-type]
    for tolerance in (0, -1, 1.1, True, float("inf")):
      with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
        CollectionCadence(((0.0,),), tolerance)
    plan = _plan()
    with self.assertRaises(ValueError):
      replace(plan, cadence=CollectionCadence(((0.0,),)))
    with self.assertRaises(ValueError):
      replace(plan, cadence=CollectionCadence((tuple(i * 0.1 for i in range(16)),)))
    with self.assertRaises(ValueError):
      replace(plan, cadence=CollectionCadence((tuple(i * 20.0 for i in range(16)),)))
    with self.assertRaises(ValueError):
      replace(plan, filters=(CollectionFilter(3, (0.5, 0.2, 0.1)),))


class CadenceJournalTests(unittest.TestCase):
  """Ownership caching cannot conceal new unresolved runs, releases or external edits."""

  def setUp(self) -> None:
    """Use a completed synthetic run and prime its ownership observations."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.journal = RunJournal(self.path)
    self.thermal_run = replace(_run(), state="completed")
    self.journal.reserve(self.thermal_run)
    self.check()

  def tearDown(self) -> None:
    """Release both database files after each case."""
    self.journal.close()
    self.temp.cleanup()

  def check(self) -> None:
    """Request ordinary optical ownership without permission to ignore an active run."""
    self.journal.check_optical_control(
      self.thermal_run.camera_description, self.thermal_run.thermal_version
    )

  def test_local_run_and_release_changes_invalidate_cached_ownership(self) -> None:
    """Neither a newly active run nor an uncertain release can reuse the idle cache."""
    self.journal.save(replace(self.thermal_run, state="running"))
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      self.check()
    self.journal.save(self.thermal_run)
    self.check()
    self.journal.save(replace(self.thermal_run, plate_release_state="uncertain"))
    with self.assertRaisesRegex(ABI7500RunError, "Resolve plate release"):
      self.check()
    self.journal.save(self.thermal_run)
    self.check()
    self.journal.reserve(replace(self.thermal_run, run_id="another-run", state="running"))
    with self.assertRaisesRegex(ABI7500RunError, "another-run"):
      self.check()

  def test_external_commit_invalidates_cached_ownership(self) -> None:
    """A separate SQLite writer cannot hide active state behind a prior validated snapshot."""
    with sqlite3.connect(self.path) as db:
      db.execute(
        "UPDATE runs SET record=?",
        (json.dumps(replace(self.thermal_run, state="running").to_dict()),),
      )
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      self.check()

  def test_external_identity_corruption_and_reopen_are_rechecked(self) -> None:
    """Read cache never substitutes for validating a changed primary key and serialized ID."""
    with sqlite3.connect(self.path) as db:
      db.execute("UPDATE runs SET run_id='corrupted'")
    with self.assertRaisesRegex(ABI7500RunError, "identity mismatch"):
      self.check()
    self.journal.close()
    with self.assertRaisesRegex(ABI7500RunError, "identity mismatch"):
      self.check()


class CadenceDriverTests(unittest.IsolatedAsyncioTestCase):
  """Use byte-scripted USB and a temporary journal; never open hardware."""

  async def asyncSetUp(self) -> None:
    """Replace the transport and clock before any acquisition code can run."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.clock = _Clock()
    self.usb = _CadenceUSB(self.clock)
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.journal = self.driver._require_journal()

  async def asyncTearDown(self) -> None:
    """Release mocked connections and SQLite ownership."""
    await self.driver.stop()
    self.temp.cleanup()

  def prepare(self, plan: CollectionPlan, *, changed: str = "", initial_position: int = 0) -> None:
    """Reserve a synthetic active run whose holds fit the recorded cadence."""
    run = _run()
    program = replace(
      run.program,
      cycles=tuple(
        replace(c, steps=tuple(replace(s, hold_time=312) for s in c.steps))
        for c in run.program.cycles
      ),
    )
    self.journal.reserve(
      replace(
        run,
        program=program,
        collection_plan=plan,
        cover_temperature=90 if "cover" in changed else None,
        cover_configured="cover" in changed,
      )
    )
    self.usb.burst_point(plan, changed=changed, initial_position=initial_position)

  async def collect(self) -> None:
    """Run with deterministic clock latencies while real asyncio handles task cancellation."""
    with (
      patch(
        "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.get_running_loop",
        return_value=self.clock,
      ),
      patch("pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=self.clock.sleep),
    ):
      await self.driver._collect_point(_RUN_ID, _POINT)

  async def test_full_cadence_and_boundary_metadata(self) -> None:
    """Sixty-five unique starts meet absolute targets; original bytes and shared checks persist."""
    plan = _plan(True)
    self.prepare(plan)
    await self.collect()
    self.assertFalse(self.usb.script)
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(collection.record.state, "complete")
    self.assertEqual(len(collection.acquisitions), 65)
    assert plan.cadence is not None
    starts = [t - self.usb.starts[0] for t in self.usb.starts]
    for actual, target in zip(starts, [t for group in plan.cadence.offsets for t in group]):
      self.assertGreaterEqual(actual + 1e-9, target)
      self.assertLessEqual(actual - target, plan.cadence.max_lateness)
    for a, actual in zip(collection.acquisitions, starts):
      self.assertEqual(a.state, "complete")
      self.assertEqual(a.observation_scope, "filter_burst")
      assert a.exposure_start_offset is not None
      self.assertAlmostEqual(a.exposure_start_offset, actual)
      self.assertEqual(a.wire_data, _IMAGE)
      self.assertEqual(CameraAcquisition.from_dict(a.to_dict()), a)
    self.assertEqual(self.journal._open().execute("SELECT version FROM format").fetchone(), (7,))
    writes = list(self.usb.writes)
    await self.driver._collect_point(_RUN_ID, _POINT)
    self.assertEqual(self.usb.writes, writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.assertEqual((await self.driver.get_collections(_RUN_ID))[0], collection)

  async def test_recorded_first_filter_transition_meets_deadline(self) -> None:
    """Replay the measured first transition followed by synthetic delays for later frames."""
    plan = _plan(True)
    usb = _RecordedBoundaryUSB(self.clock)
    self.usb = usb
    self.driver.io = cast(USB, usb)
    self.prepare(plan)
    await self.collect()
    self.assertFalse(usb.script)
    self.assertEqual(len(usb.starts), 65)
    self.assertEqual(usb.skipped_queries, 2)
    removed = sum(
      e["elapsed_to_next_command_without_poll_sleep"]
      for e in _BOUNDARY["commands"]
      if e["redundant"]
    )
    actual = usb.starts[1] - usb.starts[0]
    self.assertAlmostEqual(
      actual, _BOUNDARY["original_second_configuration_reply"] - removed, places=5
    )
    self.assertLess(actual, _BOUNDARY["target"] + 0.5)
    assert plan.cadence is not None
    for started, target in zip(usb.starts, [t for group in plan.cadence.offsets for t in group]):
      self.assertGreaterEqual(started - usb.starts[0] + 1e-9, target)
      self.assertLessEqual(started - usb.starts[0] - target, plan.cadence.max_lateness)
    self.assertTrue(
      all(
        a.state == "complete" for a in (await self.driver.get_collections(_RUN_ID))[0].acquisitions
      )
    )

  async def assert_failed_without_replay(
    self, expected_starts: int, *, completed_prefix: int = 0
  ) -> None:
    """Uncertain collection and run errors block both same-process and restart retries."""
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(collection.record.state, "uncertain")
    self.assertTrue(collection.record.error)
    self.assertTrue(all(a.state == "complete" for a in collection.acquisitions[:completed_prefix]))
    self.assertTrue(all(a.state == "uncertain" for a in collection.acquisitions[completed_prefix:]))
    self.assertEqual(len(self.usb.starts), expected_starts)
    writes = list(self.usb.writes)
    with self.assertRaises(ABI7500RunError):
      await self.driver._collect_point(_RUN_ID, _POINT)
    self.assertEqual(self.usb.writes, writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    with self.assertRaises(ABI7500RunError):
      await self.driver.collect_fluorescence(_RUN_ID)

  async def test_late_start_never_catches_up(self) -> None:
    """A slow first transfer is retained, but the late second exposure is not sent."""
    self.prepare(_plan())
    self.usb.delay_image = 1
    with self.assertRaisesRegex(ABI7500RunError, "cadence deadline"):
      await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE)
    self.assertIsNone(frames[1].exposure_start_offset)
    await self.assert_failed_without_replay(1)

  async def test_short_hold_prevents_first_exposure(self) -> None:
    """A fresh insufficient hold cannot be overridden by an earlier scheduling observation."""
    self.prepare(_plan(), changed="short-hold")
    with self.assertRaisesRegex(ABI7500RunError, "Insufficient hold"):
      await self.collect()
    await self.assert_failed_without_replay(0)

  async def test_fresh_checks_when_filter_already_selected(self) -> None:
    """Without a movement poll, read motor state afresh along with position and lamp."""
    self.prepare(_plan(), initial_position=3)
    await self.collect()
    self.assertFalse(self.usb.script)
    self.assertNotIn(
      0x69, [ThermalPacket.decode(w[6:]).command for w in self.usb.writes if w.startswith(b"RS")]
    )

  async def test_changed_motor_prevents_first_exposure(self) -> None:
    """Unknown motion state cannot be treated as the stationary observation to reuse."""
    self.prepare(_plan(), changed="motor")
    with self.assertRaisesRegex(ABI7500ProtocolError, "motor state"):
      await self.collect()
    await self.assert_failed_without_replay(0)

  async def test_wrong_position_prevents_first_exposure(self) -> None:
    """A stopped motor alone cannot establish the requested filter position."""
    self.prepare(_plan(), changed="position")
    with self.assertRaisesRegex(ABI7500RunError, "Optical readback"):
      await self.collect()
    await self.assert_failed_without_replay(0)

  async def test_changed_lamp_before_burst_prevents_exposure(self) -> None:
    """Reusing the motor poll still requires a fresh illuminated lamp readback."""
    self.prepare(_plan(), changed="before-lamp")
    with self.assertRaisesRegex(ABI7500RunError, "Optical readback"):
      await self.collect()
    await self.assert_failed_without_replay(0)

  async def check_changed_boundary(self, changed: str, message: str) -> None:
    """Reject a changed fresh observation after moving to the second filter."""
    self.prepare(_plan(True), changed=changed)
    with self.assertRaisesRegex(ABI7500RunError, message):
      await self.collect()
    await self.assert_failed_without_replay(1, completed_prefix=1)

  async def test_hold_changes_during_filter_move(self) -> None:
    """A prior valid hold does not authorize an exposure in a different repetition."""
    await self.check_changed_boundary("before-hold", "thermal boundary")

  async def test_owner_changes_during_filter_move(self) -> None:
    """Run identity is rechecked after movement even when the prior burst was valid."""
    await self.check_changed_boundary("before-owner", "thermal boundary")

  async def test_cover_changes_during_filter_move(self) -> None:
    """Fresh cover temperature is checked before the first exposure of the next burst."""
    await self.check_changed_boundary("before-cover", "cover temperature")

  async def test_persistence_delay_exhausts_in_progress_movement_budget(self) -> None:
    """Committing during motion cannot renew its deadline or authorize the next exposure."""
    self.prepare(_plan(True))
    save = self.journal.save_acquisitions

    def slow_save(acquisitions: tuple[CameraAcquisition, ...]) -> None:
      save(acquisitions)
      if all(a.state == "complete" for a in acquisitions):
        self.clock.now += 205

    with patch.object(self.journal, "save_acquisitions", side_effect=slow_save):
      with self.assertRaises(TimeoutError):
        await self.collect()
    moves = [
      ThermalPacket.decode(w[6:]).payload
      for w in self.usb.writes
      if w.startswith(b"RS") and ThermalPacket.decode(w[6:]).command == 0x69
    ]
    self.assertEqual(moves, [b"\0\1", b"\0\2"])
    await self.assert_failed_without_replay(1, completed_prefix=1)

  async def test_changed_lamp_keeps_whole_burst_uncertain(self) -> None:
    """No frame claims individually verified optics if the final shared state disagrees."""
    self.prepare(_plan(), changed="lamp")
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.collect()
    await self.assert_failed_without_replay(16)

  async def test_changed_hold_keeps_whole_burst_uncertain(self) -> None:
    """Complete pixels do not establish thermal association after a repetition change."""
    self.prepare(_plan(), changed="hold")
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.collect()
    await self.assert_failed_without_replay(16)

  async def test_changed_owner_keeps_whole_burst_uncertain(self) -> None:
    """Matching hold indices cannot hide a different controller run token."""
    self.prepare(_plan(), changed="owner")
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.collect()
    await self.assert_failed_without_replay(16)

  async def test_changed_cover_keeps_whole_burst_uncertain(self) -> None:
    """Burst mode retains the cover-temperature acceptance requirement."""
    self.prepare(_plan(), changed="cover")
    with self.assertRaisesRegex(ABI7500RunError, "cover temperature"):
      await self.collect()
    await self.assert_failed_without_replay(16)

  async def test_partial_image_is_retained_without_replay(self) -> None:
    """An interrupted third read preserves preceding full transfers and its own partial bytes."""
    self.prepare(_plan())
    self.usb.partial_image = 3
    with self.assertRaisesRegex(TimeoutError, "Truncated"):
      await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE)
    self.assertEqual(frames[1].wire_data, _IMAGE)
    self.assertGreater(len(frames[2].wire_data), 4096)
    self.assertLess(len(frames[2].wire_data), len(_IMAGE))
    await self.assert_failed_without_replay(3)

  async def test_wrong_exposure_reply_prevents_cc(self) -> None:
    """A fixed timing target never overrides a rejected exposure setting."""
    self.prepare(_plan(), changed="exposure")
    with self.assertRaisesRegex(ValueError, "exposure"):
      await self.collect()
    await self.assert_failed_without_replay(0)

  async def test_reservation_failure_does_not_overwrite_a_prior_frame(self) -> None:
    """A failed new intent must not save empty bytes over an already retained acquisition."""
    self.prepare(_plan())
    with patch.object(
      self.journal,
      "save_acquisition_and_reserve_next",
      side_effect=ABI7500RunError("Cannot reserve new exposure"),
    ):
      with self.assertRaisesRegex(ABI7500RunError, "Cannot reserve"):
        await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(len(frames), 1)
    self.assertEqual(frames[0].wire_data, _IMAGE)
    await self.assert_failed_without_replay(1)

  async def test_recorded_image_delays_still_enforce_sixth_deadline(self) -> None:
    """Unattributed recorded local gaps cannot be assumed away by a storage optimization."""
    full = _plan(True)
    assert full.cadence is not None
    self.usb = _RecordedImageUSB(self.clock)
    self.driver.io = cast(USB, self.usb)
    plan = replace(
      full,
      filters=full.filters[:2],
      cadence=CollectionCadence(full.cadence.offsets[:2]),
    )
    self.prepare(plan)
    with self.assertRaisesRegex(ABI7500RunError, "cadence deadline"):
      await self.collect()
    for actual, measured in zip(self.usb.starts, _IMAGE_TIMING["images"]):
      self.assertAlmostEqual(actual - self.usb.starts[0], measured["actual"], delta=0.001)
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(len(frames), 6)
    self.assertTrue(all(a.wire_data == _IMAGE for a in frames[:5]))
    self.assertFalse(frames[-1].wire_data)
    self.assertIsNone(frames[-1].exposure_start_offset)
    await self.assert_failed_without_replay(5, completed_prefix=1)

  async def test_every_next_camera_command_sees_committed_previous_bytes(self) -> None:
    """Independent readers see raw data and the next intent before either CJ or CC."""
    plan = _plan()
    self.prepare(plan)
    write = self.usb.write
    observed = 0

    async def inspect_write(data: bytes, timeout: Optional[float] = None) -> None:
      nonlocal observed
      if data.startswith(b"CJ"):
        observed += 1
      if data.startswith(b"CJ") or data == b"CC":
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
          rows = db.execute("SELECT acquisition_id,record FROM acquisitions").fetchall()
        saved = {key: CameraAcquisition.from_dict(json.loads(value)) for key, value in rows}
        prefix = _POINT.collection_id(_RUN_ID) + "-f3-e"
        self.assertIn(prefix + str(observed - 1), saved)
        for index in range(observed - 1):
          self.assertEqual(saved[prefix + str(index)].wire_data, _IMAGE)
          self.assertEqual(saved[prefix + str(index)].state, "uncertain")
      await write(data, timeout)

    statements: list[str] = []
    self.journal._open().set_trace_callback(statements.append)
    with patch.object(self.usb, "write", side_effect=inspect_write):
      await self.collect()
    self.assertEqual(observed, 16)
    # Collection/run state commits are counted separately from the 18 acquisition commits.
    acquisition_commits = 0
    contains_image = False
    for sql in statements:
      if sql.startswith(("INSERT INTO acquisitions", "UPDATE acquisitions")):
        contains_image = True
      if sql == "COMMIT":
        acquisition_commits += contains_image
        contains_image = False
    self.assertEqual(acquisition_commits, 18)

  async def test_completion_commit_runs_during_next_filter_motion(self) -> None:
    """Raw frames precede motion; final observations commit before motor polling and exposure."""
    self.prepare(_plan(True))
    write, save = self.usb.write, self.journal.save_acquisitions
    completed: list[int] = []

    async def inspect_move(data: bytes, timeout: Optional[float] = None) -> None:
      if data.startswith(b"RS"):
        packet = ThermalPacket.decode(data[6:])
        if packet.command == 0x69 and self.usb.starts:
          with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            frames = [
              CameraAcquisition.from_dict(json.loads(r[0]))
              for r in db.execute("SELECT record FROM acquisitions")
            ]
          self.assertEqual(len(frames), len(self.usb.starts))
          self.assertTrue(all(a.wire_data == _IMAGE for a in frames))
          self.assertTrue(any(a.state == "uncertain" for a in frames))
      await write(data, timeout)

    def inspect_commit(acquisitions: tuple[CameraAcquisition, ...]) -> None:
      if all(a.state == "complete" for a in acquisitions):
        position = acquisitions[0].optics_before.filter_position
        if position < 5:
          self.assertEqual(self.usb.last_command, 0x69)
          last = ThermalPacket.decode(
            next(w[6:] for w in reversed(self.usb.writes) if w.startswith(b"RS"))
          )
          self.assertEqual(last.payload, (position + 1).to_bytes(2, "big"))
        completed.append(position)
      save(acquisitions)

    with (
      patch.object(self.usb, "write", side_effect=inspect_move),
      patch.object(self.journal, "save_acquisitions", side_effect=inspect_commit),
    ):
      await self.collect()
    self.assertEqual(completed, [1, 2, 3, 4, 5])
    self.assertFalse(self.usb.script)

  async def test_failed_completion_commit_blocks_next_exposure(self) -> None:
    """A storage fault after a move preserves raw bytes without declaring the burst complete."""
    self.prepare(_plan(True))
    save = self.journal.save_acquisitions

    def fail_completion(acquisitions: tuple[CameraAcquisition, ...]) -> None:
      if all(a.state == "complete" for a in acquisitions):
        raise sqlite3.OperationalError("completion disk fault")
      save(acquisitions)

    with patch.object(self.journal, "save_acquisitions", side_effect=fail_completion):
      with self.assertRaisesRegex(sqlite3.OperationalError, "completion disk fault"):
        await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE)
    self.assertIsNotNone(frames[0].thermal_after)
    self.assertIsNotNone(frames[0].optics_after)
    self.assertIn("completion disk fault", frames[0].error or "")
    await self.assert_failed_without_replay(1)

  async def test_lost_move_reply_preserves_pending_burst(self) -> None:
    """An ambiguous move cannot discard raw images or permit a new exposure/replay."""
    self.prepare(_plan(True))
    write = self.usb.write

    async def lose_move(data: bytes, timeout: Optional[float] = None) -> None:
      await write(data, timeout)
      if data.startswith(b"RS"):
        packet = ThermalPacket.decode(data[6:])
        if packet.command == 0x69 and packet.payload == b"\0\2":
          raise TimeoutError("ambiguous filter move")

    with patch.object(self.usb, "write", side_effect=lose_move):
      with self.assertRaisesRegex(TimeoutError, "ambiguous filter move"):
        await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE)
    self.assertIsNotNone(frames[0].thermal_after)
    self.assertIsNotNone(frames[0].optics_after)
    self.assertIn("ambiguous filter move", frames[0].error or "")
    await self.assert_failed_without_replay(1)

  async def test_final_collection_fault_preserves_committed_burst(self) -> None:
    """A later collection-status fault cannot revert already committed frame completion."""
    self.prepare(_plan())
    write = self.usb.write
    final_id = f"{_POINT.collection_id(_RUN_ID)}-f3-e15"

    async def lose_final_status(data: bytes, timeout: Optional[float] = None) -> None:
      await write(data, timeout)
      if data.startswith(b"RS") and ThermalPacket.decode(data[6:]).command == 0x29:
        final = self.journal.get_acquisition(final_id)
        if final is not None and final.state == "complete":
          raise TimeoutError("lost final collection status")

    with patch.object(self.usb, "write", side_effect=lose_final_status):
      with self.assertRaisesRegex(TimeoutError, "lost final collection status"):
        await self.collect()
    await self.assert_failed_without_replay(16, completed_prefix=16)

  async def test_cancelled_image_read_retains_returned_fragment(self) -> None:
    """Wait for in-flight USB, retain its bytes, then propagate cancellation without another CC."""
    self.prepare(_plan())
    read = self.usb.read
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_read(size: int, timeout: Optional[int] = None) -> bytes:
      if self.usb.reading_image and self.usb.image_bytes == 7:
        started.set()
        await release.wait()
      return await read(size, timeout)

    with patch.object(self.usb, "read", side_effect=blocked_read):
      task = asyncio.create_task(self.collect())
      await started.wait()
      task.cancel()
      await _REAL_SLEEP(0)
      self.assertFalse(task.done())
      release.set()
      with self.assertRaises(asyncio.CancelledError):
        await task
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE[:16391])
    await self.assert_failed_without_replay(1)

  async def test_bounded_bulk_reads_retain_exact_image_and_tail(self) -> None:
    """A full image uses ten bounded reads without consuming bytes from a later command."""

    def image() -> None:
      for offset in range(0, len(_IMAGE), 16384):
        chunk = _IMAGE[offset : offset + 16384]
        self.usb.expect_read(chunk)

    with patch.object(self.usb, "image", side_effect=image):
      self.prepare(_plan())
    sizes: list[int] = []
    read = self.usb.read

    async def inspect_read(size: int, timeout: Optional[int] = None) -> bytes:
      if self.usb.reading_image:
        sizes.append(size)
      return await read(size, timeout)

    with patch.object(self.usb, "read", side_effect=inspect_read):
      await self.collect()
    self.assertEqual(sizes, ([16384] * 9 + [9856]) * 16)
    self.assertFalse(self.usb.script)
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertTrue(all(a.wire_data == _IMAGE for a in frames))

  async def check_bad_bulk_fragment(self, *, oversized: bool) -> None:
    """Reject malformed fragments while retaining only earlier valid data."""
    self.prepare(_plan())
    read = self.usb.read

    async def invalid_read(size: int, timeout: Optional[int] = None) -> bytes:
      if self.usb.reading_image and self.usb.image_bytes == 7:
        return bytes(size + 1) if oversized else b""
      return await read(size, timeout)

    with patch.object(self.usb, "read", side_effect=invalid_read):
      with self.assertRaisesRegex(ABI7500ProtocolError, "image fragment"):
        await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE[:7])
    await self.assert_failed_without_replay(1)

  async def test_empty_bulk_fragment(self) -> None:
    """An empty read cannot be mistaken for successful image completion."""
    await self.check_bad_bulk_fragment(oversized=False)

  async def test_oversized_bulk_fragment(self) -> None:
    """An overlong read cannot consume a later response or enter the retained frame."""
    await self.check_bad_bulk_fragment(oversized=True)

  async def test_download_deadline_retains_last_fragment(self) -> None:
    """Larger reads retain returned bytes but do not extend the shared transfer deadline."""
    self.prepare(_plan())
    read = self.usb.read

    async def delayed_read(size: int, timeout: Optional[int] = None) -> bytes:
      delayed = self.usb.reading_image and self.usb.image_bytes == 7
      result = await read(size, timeout)
      if delayed:
        self.clock.now += self.driver.command_timeout
      return result

    with patch.object(self.usb, "read", side_effect=delayed_read):
      with self.assertRaises(TimeoutError):
        await self.collect()
    frames = (await self.driver.get_collections(_RUN_ID))[0].acquisitions
    self.assertEqual(frames[0].wire_data, _IMAGE[:16391])
    await self.assert_failed_without_replay(1)

  async def test_cancellation_waits_for_pending_usb_without_another_cc(self) -> None:
    """Cancellation settles an in-flight operation before releasing ownership."""
    self.prepare(_plan())
    self.usb.block_exposure = 2
    task = asyncio.create_task(self.collect())
    await self.usb.block_started.wait()
    task.cancel()
    await _REAL_SLEEP(0)
    self.assertFalse(task.done())
    self.usb.unblock.set()
    with self.assertRaises(asyncio.CancelledError):
      await task
    await self.assert_failed_without_replay(2)
