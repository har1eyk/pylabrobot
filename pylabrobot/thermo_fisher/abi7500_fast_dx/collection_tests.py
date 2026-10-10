"""Exercise scheduled acquisition against captured framing and explicit synthetic run progress."""

import asyncio
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Optional, cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .acquisition import CameraExposure, OpticalState
from .acquisition_tests import _IMAGE
from .camera import CameraGeometry
from .collection import (
  CollectionFilter,
  CollectionPlan,
  CollectionPoint,
  CollectionRecord,
  FluorescenceCollection,
)
from .driver import ABI7500FastDx
from .fluorescence_tests import _VECTORS as _FLUORESCENCE_VECTORS
from .fluorescence_tests import _background, _frames, _uniformity
from .image_processing_tests import _acquisition
from .protocol import ThermalPacket
from .run import ABI7500RunError, ThermalRun
from .run_tests import _BASELINE, _PROGRAM, _RUN_ID, _checkpoint, _history, _running, _RunUSB
from .status import ABI7500Status

_PLAN = CollectionPlan(((1, 1),), (CollectionFilter(1, (0.540, 0.180, 0.060)),))
_VECTORS = json.loads(
  (Path(__file__).parent / "test_data/collection_schedule_vectors.json").read_text()
)


def _status(
  repetition: int = 1, remaining: int = 45, *, state: int = 4, phase: int = 2
) -> ABI7500Status:
  """Retain a captured run token while explicitly varying its scheduling fields."""
  status = _running("acquire").last_status
  assert status is not None
  data = bytearray(status.raw_payload)
  data[1] = state
  data[20:24] = remaining.to_bytes(4, "big")
  data[28:32] = bytes((1, 1, phase, repetition))
  return ABI7500Status(bytes(data))


def _run() -> ThermalRun:
  """Create an already-owned optical run without submitting a hardware start."""
  return replace(_running("acquire"), collection_plan=_PLAN, last_status=_status())


class _CollectionUSB(_RunUSB):
  """Byte-exact thermal and camera script, with an optional lost exposure response."""

  def __init__(self) -> None:
    """Start a disconnected transport with optional per-exposure failure injection."""
    super().__init__()
    self.fail_exposure: Optional[int] = None
    self.block_exposure: Optional[int] = None

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Lose an acknowledgement after the designated CC without permitting another attempt."""
    if self.fail_exposure is not None and self.writes.count(b"CC") == self.fail_exposure:
      raise TimeoutError("Interrupted scheduled exposure")
    if self.block_exposure is not None and self.writes.count(b"CC") == self.block_exposure:
      self.block_exposure = None
      self.block_started.set()
      await self.unblock.wait()
    return await super().read(size, timeout)

  def optics(self, position: int = 1, *, power: int = 1) -> None:
    """Script the three native optical readbacks in their actual driver order."""
    self.exchange(0x6C, b"\x00\x00")
    self.exchange(0x6B, position.to_bytes(2, "big"))
    self.exchange(0x66, b"\x00\x01" + power.to_bytes(2, "big"))

  def observe(self, status: ABI7500Status) -> None:
    """Confirm identity and independent thermal history around a status observation."""
    self.identity()
    self.observation(status.raw_payload, _history("acquire", _RUN_ID))

  def image(self) -> None:
    """Split the captured frame across an odd byte boundary and bounded USB reads."""
    self.expect_read(_IMAGE[:7], 4096)
    offset = 7
    while offset < len(_IMAGE):
      size = min(4096, len(_IMAGE) - offset)
      self.expect_read(_IMAGE[offset : offset + size], size)
      offset += size

  def point(
    self,
    repetition: int = 1,
    *,
    crossed: bool = False,
    move: bool = True,
    park: bool = False,
    home: bool = False,
    parked_position: int = 0,
    parked_power: int = 1,
    crossed_park: bool = False,
  ) -> None:
    """Script three exposures; an optional repetition change invalidates the final frame."""
    before = _status(repetition)
    self.observe(before)
    self.optics(0 if move else 1)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")
    self.exchange(0x29, before.raw_payload)
    if move:
      self.exchange(0x69, request=b"\x00\x01")
      self.exchange(0x6C, b"\x00\x01")
      self.exchange(0x6C, b"\x00\x00")
    self.optics()
    for index, duration in enumerate(_PLAN.filters[0].exposures):
      exposure = CameraExposure(duration)
      self.expect_write(exposure.encode())
      self.expect_read(exposure.milliseconds.to_bytes(2, "little"))
      self.exchange(0x29, before.raw_payload)
      self.expect_write(b"CC")
      self.expect_write(b"CH   5")
      self.expect_read(b"not r")
      self.expect_write(b"CH   5")
      self.expect_read(b"ready")
      self.expect_write(CameraGeometry(321, 245).encode_read())
      self.image()
      self.optics()
      after = _status(repetition + int(crossed and index == 2), remaining=30 - index)
      self.exchange(0x29, after.raw_payload)
    if not crossed:
      if park:
        self.optics()
        if home:
          self.exchange(0x67)
        else:
          self.exchange(0x69, request=b"\x00\x00")
        self.exchange(0x6C, b"\x00\x01")
        self.exchange(0x6C, b"\x00\x00")
        self.optics(parked_position, power=parked_power)
        if parked_position != 0 or parked_power != 1:
          return
      self.exchange(0x29, _status(repetition + int(crossed_park), remaining=27).raw_payload)


class CollectionPlanTests(unittest.TestCase):
  """Validate native scheduling evidence and reject unsupported plans without hardware."""

  def test_captured_home_framing_and_asynchronous_motor_observations(self) -> None:
    """Original vendor bytes establish empty home acknowledgements and later busy/idle replies."""
    fixture = json.loads((Path(__file__).parent / "test_data/filter_home_vectors.json").read_text())
    replies = []
    for pair in fixture["pairs"]:
      request = ThermalPacket.decode(bytes.fromhex(pair["request"]["packet_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(pair["reply"]["packet_hex"]))
      self.assertEqual(request.sequence, reply.sequence)
      self.assertEqual(request.command, reply.command)
      self.assertEqual(reply.error_code, 0)
      self.assertEqual(request.payload, b"")
      replies.append((reply.command, reply.payload))
    self.assertEqual(replies, [(0x67, b""), (0x67, b""), (0x6C, b"\0\1"), (0x6C, b"\0\0")])

  def test_homing_plan_ordinals_and_legacy_compatibility(self) -> None:
    """Homing is explicit, immutable and limited to points in the uploaded program."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(2,))
    self.assertEqual(CollectionPlan.from_dict(plan.to_dict()), plan)
    self.assertEqual(len(plan.points(_PROGRAM)), 2)
    legacy = _PLAN.to_dict()
    del legacy["home_after_collections"]
    self.assertEqual(CollectionPlan.from_dict(legacy), _PLAN)
    for invalid in ((0,), (True,), (2, 1), (1, 1), [1]):
      with self.subTest(invalid=invalid), self.assertRaises(ValueError):
        replace(plan, home_after_collections=invalid)  # type: ignore[arg-type]
    with self.assertRaisesRegex(ValueError, "requires"):
      replace(plan, park_between_collections=False)
    with self.assertRaisesRegex(ValueError, "does not exist"):
      replace(plan, home_after_collections=(3,)).points(_PROGRAM)

  def test_parking_plan_and_legacy_records(self) -> None:
    """Persist parking intent while leaving historic acquisitions unchanged."""
    plan = replace(_PLAN, park_between_collections=True)
    self.assertEqual(CollectionPlan.from_dict(plan.to_dict()), plan)
    legacy = _PLAN.to_dict()
    del legacy["park_between_collections"]
    self.assertEqual(CollectionPlan.from_dict(legacy), _PLAN)
    with self.assertRaisesRegex(ValueError, "boolean"):
      replace(_PLAN, park_between_collections=1)  # type: ignore[arg-type]

  def test_ubuntu_frames_remain_valid_after_later_thermal_cancellation(self) -> None:
    """Thirty captured frame boundaries prove both reads, separately from thermal completion."""
    evidence = json.loads(
      (Path(__file__).parent / "test_data/ubuntu_sync_vectors.json").read_text()
    )
    run = ThermalRun.from_dict(evidence["final_run"])
    assert run.collection_plan is not None and run.history is not None
    self.assertEqual(run.state, "cancelled")
    self.assertEqual(run.history.outcome, "aborted")
    self.assertIsNotNone(run.collection_error)
    points = run.collection_plan.points(run.program)
    self.assertEqual(len(points), 2)
    for point, captured in zip(points, evidence["collections"]):
      record = CollectionRecord.from_dict(captured["record"])
      self.assertEqual(record.point, point)
      self.assertEqual(record.state, "complete")
      self.assertEqual(record.before.remaining_hold_time, 44)
      self.assertEqual(len(captured["frames"]), 15)
      self.assertEqual(tuple(f["id"] for f in captured["frames"]), record.acquisition_ids)
      self.assertEqual(
        [f["filter"] for f in captured["frames"]], [p for p in range(1, 6) for _ in range(3)]
      )
      for frame in captured["frames"]:
        for key in ("thermal_before", "thermal_after"):
          status = ABI7500Status(bytes.fromhex(frame[key]))
          self.assertTrue(point.matches(status))
          self.assertEqual(status.run_start_record, run.start_record)
          self.assertEqual(status.sample_temperature, 30)

  def test_collection_processing_preserves_indices_and_provenance(self) -> None:
    """Recorded patches keep their filter/cycle values; synthetic metadata tests association."""
    for vector in _FLUORESCENCE_VECTORS:
      frames = _frames(vector)
      status = _status(vector["cycle"])
      point = CollectionPoint(1, vector["cycle"], 1)
      optics = OpticalState(vector["physical_filter"].to_bytes(2, "big"), b"\0\0", b"\0\1\0\1")
      template = _acquisition()
      acquisitions = tuple(
        replace(
          template,
          acquisition_id=f"synthetic-{i}",
          exposure=frame.exposure,
          exposure_reply=frame.exposure.milliseconds.to_bytes(2, "little"),
          wire_data=frame.source.wire_data,
          thermal_before=status,
          thermal_after=status,
          optics_before=optics,
          optics_after=optics,
        )
        for i, frame in enumerate(frames)
      )
      record = CollectionRecord(
        "synthetic",
        point,
        vector["cycle"],
        status,
        tuple(a.acquisition_id for a in acquisitions),
        "complete",
        status,
      )
      collection = FluorescenceCollection(record, acquisitions)
      background = _background(vector["background_offset"], vector["background_slope"])
      uniformity = _uniformity(vector["uniformity_factor"])
      masks = (frames[0].mask,)
      result = collection.filter_results(background, uniformity, masks)[0]
      self.assertEqual(
        (result.stage, result.cycle, result.collection_point), (2, vector["cycle"], vector["cycle"])
      )
      self.assertEqual(result.filter, vector["saved_filter"])
      value = result.measurements[0]
      self.assertEqual(value.raw_value, vector["raw_value"])
      self.assertEqual(struct.pack(">f", value.corrected_value).hex(), vector["corrected_bits"])
      self.assertEqual(result.frames[0].source.wire_data, acquisitions[0].wire_data)
      with self.assertRaisesRegex(ValueError, "ordered"):
        replace(collection, acquisitions=tuple(reversed(acquisitions))).filter_results(
          background, uniformity, masks
        )
      crossed = replace(acquisitions[0], thermal_after=_status(vector["cycle"] + 1))
      with self.assertRaisesRegex(ValueError, "observations do not match"):
        replace(collection, acquisitions=(crossed, *acquisitions[1:])).filter_results(
          background, uniformity, masks
        )

  def test_reference_scheduling_evidence(self) -> None:
    """Both original reads stay within one hold and begin inside the native measured budget."""
    for vector in _VECTORS["vectors"]:
      point = CollectionPoint(
        vector["expected_group"], vector["expected_repetition"], vector["expected_step"]
      )
      before = ABI7500Status(bytes.fromhex(vector["before"]["reply"]["payload_hex"]))
      after = ABI7500Status(bytes.fromhex(vector["after"]["reply"]["payload_hex"]))
      self.assertTrue(point.matches(before))
      self.assertTrue(point.matches(after))
      self.assertEqual(before.remaining_hold_time, 24)
      assert before.remaining_hold_time is not None
      self.assertEqual(after.remaining_hold_time, 1)
      self.assertEqual(before.run_start_record, after.run_start_record)
      self.assertLessEqual(
        before.remaining_hold_time * 1000,
        _VECTORS["native_estimated_read_ms"] + _VECTORS["native_margin_ms"],
      )
      self.assertEqual(
        [int(i["filter_payload"], 16) for i in vector["images"]],
        [p for p in range(1, 6) for _ in range(3)],
      )

  def test_plan_roundtrip_and_effective_holds(self) -> None:
    """Check every repeated hold, explicit ordering and the stable point identities."""
    self.assertEqual(CollectionPlan.from_dict(_PLAN.to_dict()), _PLAN)
    self.assertEqual(_PLAN.points(_PROGRAM), (CollectionPoint(1, 1, 1), CollectionPoint(1, 2, 1)))
    with self.assertRaisesRegex(ValueError, "entire collection window"):
      replace(_PLAN, read_window=61).points(_PROGRAM)
    with self.assertRaises(ValueError):
      replace(_PLAN, steps=((0, 2),)).points(_PROGRAM)
    with self.assertRaises(ValueError):
      replace(_PLAN, steps=((1, 1), (1, 1)))
    with self.assertRaises(ValueError):
      CollectionFilter(0, (0.54, 0.18, 0.06))
    with self.assertRaises(ValueError):
      CollectionFilter(1, (0.54, 0.54, 0.06))
    with self.assertRaises(ValueError):
      replace(_PLAN, filters=(_PLAN.filters[0], _PLAN.filters[0]))
    old = _run().to_dict()
    del old["collection_plan"], old["collection_error"]
    self.assertIsNone(ThermalRun.from_dict(old).collection_plan)


class CollectionDriverTests(unittest.IsolatedAsyncioTestCase):
  """No real device is opened; ambiguous exposures and late windows must never be retried."""

  async def asyncSetUp(self) -> None:
    """Use a temporary durable journal and replace USB before connecting."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _CollectionUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.driver._geometry = CameraGeometry(321, 245)
    self.journal = self.driver._require_journal()
    self.journal.reserve(_run())
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Release the journal and mocks even when a scripted exchange fails."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def test_scheduler_loads_images_only_after_thermal_completion(self) -> None:
    """Polling saved progress must not repeatedly decode the growing camera archive."""
    self.usb.observe(_status(remaining=60))
    self.usb.observe(_status())
    self.usb.point()
    self.usb.observe(_status(remaining=10))
    self.usb.observe(_status(2, remaining=60))
    self.usb.observe(_status(2))
    self.usb.point(2, move=False)
    self.usb.observe(_status(2, state=6, phase=0))
    self.usb.observe(_status(2, state=2, phase=0))
    read_collections = self.driver._read_collections

    def read_completed(run: ThermalRun) -> tuple[FluorescenceCollection, ...]:
      """Fail if the scheduler requests original image bytes while still observing the run."""
      self.assertEqual(run.state, "completed")
      return read_collections(run)

    with patch.object(self.driver, "_read_collections", side_effect=read_completed) as read:
      collections = await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(read.call_count, 1)
    self.assertEqual([c.record.ordinal for c in collections], [1, 2])
    self.assertTrue(all(a.wire_data == _IMAGE for c in collections for a in c.acquisitions))
    self.assertEqual(self.usb.writes.count(b"CC"), 6)
    self.assertFalse(self.usb.script)

  async def test_collection_records_preserve_partial_progress_without_loading_images(self) -> None:
    """Offline progress survives restart and includes failed intent without rereading pixels."""
    self.usb.point()
    await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.usb.point(2, move=False)
    self.usb.fail_exposure = 5
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 2, 1))
    expected = tuple(c.record for c in await self.driver.get_collections(_RUN_ID))
    self.assertEqual([r.state for r in expected], ["complete", "uncertain"])
    self.assertIn("TimeoutError", expected[1].error or "")
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    with patch.object(
      self.driver._require_journal(), "get_acquisition", side_effect=AssertionError("Image loaded")
    ):
      self.assertEqual(await self.driver.get_collection_records(_RUN_ID), expected)
    self.assertFalse(self.driver.is_connected)
    self.assertEqual(self.usb.writes, writes)

  async def test_collection_records_validate_run_without_usb(self) -> None:
    """An empty optical run differs from an unknown ID or a thermal-only run."""
    self.assertEqual(await self.driver.get_collection_records(_RUN_ID), ())
    with self.assertRaisesRegex(ABI7500RunError, "Unknown run"):
      await self.driver.get_collection_records("missing")
    with self.assertRaises(ValueError):
      await self.driver.get_collection_records("invalid id")
    self.journal.save(replace(_run(), collection_plan=None))
    with self.assertRaisesRegex(ABI7500RunError, "no optical collection plan"):
      await self.driver.get_collection_records(_RUN_ID)
    self.assertFalse(self.usb.writes)

  async def test_scheduler_rejects_a_gap_in_saved_collections(self) -> None:
    """A later checkpoint cannot shift the next point or cause a missing point to be replayed."""
    point = CollectionPoint(1, 2, 1)
    record = CollectionRecord(
      _RUN_ID,
      point,
      2,
      _status(2),
      tuple(f"{point.collection_id(_RUN_ID)}-f1-e{i}" for i in range(3)),
    )
    self.journal.reserve_collection(record)
    self.journal.save_collection(replace(record, state="complete", after=_status(2)))
    self.usb.observe(_status(2))
    with self.assertRaisesRegex(ABI7500RunError, "gap"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes.count(b"CC"), 0)
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)

  async def test_collection_records_reject_changed_plan_association(self) -> None:
    """Checkpoint ordinals, frame identities and homing intent must still match the run."""
    point = CollectionPoint(1, 1, 1)
    record = CollectionRecord(
      _RUN_ID,
      point,
      1,
      _status(),
      tuple(f"{point.collection_id(_RUN_ID)}-f1-e{i}" for i in range(3)),
    )
    self.journal.reserve_collection(record)
    for invalid in (
      replace(record, ordinal=2),
      replace(record, acquisition_ids=tuple(reversed(record.acquisition_ids))),
      replace(record, homing_requested=True),
    ):
      with self.subTest(record=invalid):
        self.journal.save_collection(invalid)
        with self.assertRaisesRegex(ABI7500RunError, "disagrees"):
          await self.driver.get_collection_records(_RUN_ID)
    self.assertFalse(self.usb.writes)

  async def test_scheduler_rejects_uncertain_checkpoint_without_loading_frames(self) -> None:
    """A crash after reservation cannot silently resume a collection without an error field."""
    point = CollectionPoint(1, 1, 1)
    record = CollectionRecord(
      _RUN_ID,
      point,
      1,
      _status(),
      tuple(f"{point.collection_id(_RUN_ID)}-f1-e{i}" for i in range(3)),
    )
    self.journal.reserve_collection(record)
    self.usb.observe(_status())
    with patch.object(self.journal, "get_acquisition", side_effect=AssertionError("Image loaded")):
      with self.assertRaisesRegex(ABI7500RunError, "uncertain collection"):
        await self.driver.collect_fluorescence(_RUN_ID)
      self.assertEqual(await self.driver.get_collection_records(_RUN_ID), (record,))
    self.assertEqual(self.usb.writes.count(b"CC"), 0)
    self.assertFalse(self.usb.script)

  async def test_collection_duplicate_prevention_and_restart(self) -> None:
    """Concurrent requests for the same point create exactly three CC commands."""
    self.usb.point()
    point = CollectionPoint(1, 1, 1)
    await asyncio.gather(
      self.driver._collect_point(_RUN_ID, point), self.driver._collect_point(_RUN_ID, point)
    )
    saved = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(saved.record.state, "complete")
    self.assertEqual(CollectionRecord.from_dict(saved.record.to_dict()), saved.record)
    self.assertEqual(len(saved.acquisitions), 3)
    self.assertTrue(all(a.wire_data == _IMAGE for a in saved.acquisitions))
    self.assertEqual(self.usb.writes.count(b"CC"), 3)

    self.assertFalse(self.usb.script)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    await self.driver._collect_point(_RUN_ID, point)
    self.assertEqual((await self.driver.get_collections(_RUN_ID))[0], saved)
    self.assertEqual(self.usb.writes.count(b"CC"), 3)

  async def test_parking_between_collections_keeps_lamp_on(self) -> None:
    """Each burst parks before completion; cached retrieval cannot repeat a move or image."""
    plan = replace(_PLAN, park_between_collections=True)
    self.journal.save(replace(_run(), collection_plan=plan))
    for repetition in (1, 2):
      self.usb.observe(_status(repetition))
      self.usb.point(repetition, park=True)
    self.usb.observe(_status(2, state=2, phase=0))
    records = await self.driver.collect_fluorescence(_RUN_ID)
    expected = OpticalState(b"\0\0", b"\0\0", b"\0\1\0\1")
    for collection in records:
      self.assertEqual(collection.record.parked_optics, expected)
      self.assertEqual(CollectionRecord.from_dict(collection.record.to_dict()), collection.record)
    self.assertEqual(self.usb.writes.count(b"CC"), 6)
    self.assertEqual(self.usb.commands().count(0x69), 4)
    self.assertTrue(all(c not in self.usb.commands() for c in (0x64, 0x65, 0x67)))
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    self.assertEqual(await self.driver.collect_fluorescence(_RUN_ID), records)
    self.assertEqual(self.usb.writes, writes)

  async def test_mixed_parking_and_homing_are_durable_and_not_replayed(self) -> None:
    """One home replaces the second park; restart cannot repeat either command or a frame."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(2,))
    self.journal.save(replace(_run(), collection_plan=plan))
    for repetition in (1, 2):
      self.usb.observe(_status(repetition))
      self.usb.point(repetition, park=True, home=repetition == 2)
    self.usb.observe(_status(2, state=2, phase=0))
    records = await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual([c.record.homing_requested for c in records], [False, True])
    for c in records:
      self.assertEqual(CollectionRecord.from_dict(c.record.to_dict()), c.record)
      self.assertEqual(c.record.parked_optics, OpticalState(b"\0\0", b"\0\0", b"\0\1\0\1"))
    self.assertEqual(self.usb.commands().count(0x67), 1)
    self.assertEqual(self.usb.commands().count(0x69), 3)
    self.assertEqual(self.usb.writes.count(b"CC"), 6)
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    self.assertEqual(await self.driver.collect_fluorescence(_RUN_ID), records)
    with self.assertRaisesRegex(ABI7500RunError, "different program or collection plan"):
      await self.driver.start_run(
        _RUN_ID, _PROGRAM, collection_plan=replace(plan, home_after_collections=(1, 2))
      )
    self.assertEqual(self.usb.writes, writes)

  async def test_lost_home_acknowledgement_blocks_replay_after_restart(self) -> None:
    """An uncertain home preserves all three frames and cannot be retried with the run ID."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(1,))
    self.journal.save(replace(_run(), collection_plan=plan))
    self.usb.point(park=True, home=True)
    self.usb.fail_command = 0x67
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    c = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertTrue(c.record.homing_requested)
    self.assertEqual(c.record.state, "uncertain")
    self.assertTrue(all(a.state == "complete" and a.wire_data == _IMAGE for a in c.acquisitions))
    self.assertEqual(self.usb.commands().count(0x67), 1)
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)

  async def test_home_requires_position_zero_readback(self) -> None:
    """A stopped motor at the wrong position does not establish successful homing."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(1,))
    self.journal.save(replace(_run(), collection_plan=plan))
    self.usb.point(park=True, home=True, parked_position=1)
    with self.assertRaisesRegex(ABI7500RunError, "parking readback"):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertEqual(self.usb.commands().count(0x67), 1)
    self.assertFalse(self.usb.script)
    self.assertEqual((await self.driver.get_collections(_RUN_ID))[0].record.state, "uncertain")

  async def test_home_motion_timeout_does_not_retry(self) -> None:
    """An acknowledged home that does not settle leaves the run's optics uncertain."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(1,))
    self.journal.save(replace(_run(), collection_plan=plan))
    self.usb.point(move=False, park=True, home=True)
    with patch.object(self.driver, "_wait_filter", new=AsyncMock(side_effect=TimeoutError)):
      with self.assertRaises(TimeoutError):
        await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    record = (await self.driver.get_collections(_RUN_ID))[0].record
    self.assertEqual(record.state, "uncertain")
    self.assertTrue(record.homing_requested)
    self.assertIsNone(record.parked_optics)
    self.assertEqual(self.usb.commands().count(0x67), 1)

  async def test_task_cancellation_during_home_preserves_intent(self) -> None:
    """Cancellation waits for in-flight USB to finish, then permanently records uncertainty."""
    plan = replace(_PLAN, park_between_collections=True, home_after_collections=(1,))
    self.journal.save(replace(_run(), collection_plan=plan))
    self.usb.point(park=True, home=True)
    self.usb.block_command = 0x67
    task = asyncio.create_task(self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1)))
    await asyncio.wait_for(self.usb.block_started.wait(), timeout=2)
    task.cancel()
    self.usb.unblock.set()
    with self.assertRaises(asyncio.CancelledError):
      await task
    c = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertTrue(c.record.homing_requested)
    self.assertEqual(c.record.state, "uncertain")
    self.assertEqual(len(c.acquisitions), 3)
    self.assertEqual(self.usb.commands().count(0x67), 1)
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)

  async def test_failed_parking_retains_images_and_blocks_replay(self) -> None:
    """A wrong park readback invalidates the collection without deleting completed frames."""
    self.journal.save(
      replace(_run(), collection_plan=replace(_PLAN, park_between_collections=True))
    )
    self.usb.point(park=True, parked_position=1)
    with self.assertRaisesRegex(ABI7500RunError, "parking readback"):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(collection.record.state, "uncertain")
    self.assertIsNone(collection.record.parked_optics)
    self.assertTrue(
      all(a.state == "complete" and a.wire_data == _IMAGE for a in collection.acquisitions)
    )
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)
    self.assertFalse(self.usb.script)

  async def test_lamp_change_during_parking_invalidates_collection(self) -> None:
    """Position alone is insufficient if lamp power changed during the operation."""
    self.journal.save(
      replace(_run(), collection_plan=replace(_PLAN, park_between_collections=True))
    )
    self.usb.point(park=True, parked_power=0)
    with self.assertRaisesRegex(ABI7500RunError, "parking readback"):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertEqual((await self.driver.get_collections(_RUN_ID))[0].record.state, "uncertain")
    self.assertFalse(self.usb.script)

  async def test_thermal_boundary_during_parking_retains_park_evidence(self) -> None:
    """Parking must fit inside the hold, not merely the camera exposures."""
    self.journal.save(
      replace(_run(), collection_plan=replace(_PLAN, park_between_collections=True))
    )
    self.usb.point(park=True, crossed_park=True)
    with self.assertRaisesRegex(ABI7500RunError, "thermal boundary"):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    record = (await self.driver.get_collections(_RUN_ID))[0].record
    self.assertEqual(record.state, "uncertain")
    self.assertIsNotNone(record.parked_optics)
    self.assertFalse(self.usb.script)

  async def test_ambiguous_park_move_is_never_reissued(self) -> None:
    """Losing the park acknowledgement cannot trigger another move or a repeated burst."""
    self.journal.save(
      replace(_run(), collection_plan=replace(_PLAN, park_between_collections=True))
    )
    self.usb.point(move=False, park=True)
    self.usb.fail_command = 0x69
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(len(collection.acquisitions), 3)
    self.assertEqual(collection.record.state, "uncertain")
    self.assertEqual(self.usb.commands().count(0x69), 1)
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)

  async def test_two_cycle_scheduler_and_finishing_transition(self) -> None:
    """Serve two distinct repetitions and wait through the captured state-6 finish transition."""
    self.usb.observe(_status())
    self.usb.point()
    self.usb.observe(_status(2))
    self.usb.point(2, move=False)
    self.usb.observe(_status(2, state=6, phase=0))
    self.usb.observe(_status(2, state=2, phase=0))
    result = await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual([r.record.point.repetition for r in result], [1, 2])
    self.assertEqual([r.record.ordinal for r in result], [1, 2])
    self.assertEqual(self.usb.writes.count(b"CC"), 6)
    self.assertFalse(self.usb.script)
    writes = len(self.usb.writes)
    self.assertEqual(await self.driver.collect_fluorescence(_RUN_ID), result)
    self.assertEqual(len(self.usb.writes), writes)

  async def test_captured_startup_transition_reconciles_without_exposure(self) -> None:
    """A real status read spanning Start must not permanently fail the collection scheduler."""
    evidence = json.loads(
      (Path(__file__).parent / "test_data/ubuntu_sync_startup_vector.json").read_text()
    )
    observed = evidence["observations"]
    run = replace(
      ThermalRun.from_dict(evidence["final_run"]),
      state="starting",
      start_record=None,
      last_status=ABI7500Status(bytes.fromhex(observed[0]["before_status_hex"])),
      history=None,
      cancel_requested=False,
      idle_temperature_set=False,
      collection_error=None,
    )
    self.journal.save(replace(_run(), state="completed"))
    self.journal.reserve(run)
    for observation in (observed[0], observed[1], observed[-1]):
      self.usb.identity()
      self.usb.exchange(0x29, bytes.fromhex(observation["before_status_hex"]))
      self.usb.exchange(0x19, bytes.fromhex(observation["history_hex"]))
      self.usb.exchange(0x29, bytes.fromhex(observation["after_status_hex"]))
    # The captured session was cancelled before any collection. The scheduler must pass the
    # transient uncertain observation and coherent active observation before reporting that end.
    with self.assertRaisesRegex(ABI7500RunError, "ended without every planned collection"):
      await self.driver.collect_fluorescence(run.run_id)
    self.assertFalse(self.usb.script)
    self.assertEqual(self.usb.writes.count(b"CC"), 0)
    final = self.journal.get(run.run_id)
    assert final is not None
    self.assertEqual(final.state, "aborted")
    self.assertIsNotNone(final.start_record)

  async def test_captured_optical_completion_cannot_restart(self) -> None:
    """Replay the real final observations and retain completion without another Start or Stop."""
    evidence = json.loads(
      (Path(__file__).parent / "test_data/ubuntu_sync_completed_vectors.json").read_text()
    )
    completed = ThermalRun.from_dict(evidence["final_run"])
    assert completed.collection_plan is not None and completed.history is not None
    self.assertEqual(completed.state, "completed")
    self.assertEqual(completed.history.outcome, "completed")
    self.assertIsNone(completed.collection_error)
    self.assertFalse(completed.cancel_requested)
    self.assertFalse(completed.idle_temperature_set)
    points = completed.collection_plan.points(completed.program)
    self.assertEqual(len(points), 2)
    for point, captured in zip(points, evidence["collections"]):
      record = CollectionRecord.from_dict(captured["record"])
      self.assertEqual(record.point, point)
      self.assertEqual(record.state, "complete")
      self.assertEqual(len(captured["frames"]), 15)
      self.assertEqual(tuple(f["id"] for f in captured["frames"]), record.acquisition_ids)
      self.assertEqual(
        [f["filter"] for f in captured["frames"]], [p for p in range(1, 6) for _ in range(3)]
      )
      for frame in captured["frames"]:
        for key in ("thermal_before", "thermal_after"):
          status = ABI7500Status(bytes.fromhex(frame[key]))
          self.assertTrue(point.matches(status))
          self.assertEqual(status.run_start_record, completed.start_record)
          self.assertEqual(status.sample_temperature, 30)
    observations = evidence["completion_observations"]
    run = replace(
      completed,
      state="running",
      history=None,
      last_status=ABI7500Status(bytes.fromhex(observations[0]["before_status_hex"])),
    )
    self.journal.save(replace(_run(), state="completed"))
    self.journal.reserve(run)
    for observation in observations:
      self.usb.identity()
      self.usb.observation(
        bytes.fromhex(observation["before_status_hex"]),
        bytes.fromhex(observation["history_hex"]),
        bytes.fromhex(observation["after_status_hex"]),
      )
      run = await self.driver.get_run(run.run_id)
    self.assertEqual(run, completed)
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    self.assertEqual(
      await self.driver.start_run(run.run_id, run.program, collection_plan=run.collection_plan),
      completed,
    )
    self.assertEqual(await self.driver.get_run(run.run_id), completed)
    self.assertEqual(await self.driver.cancel_run(run.run_id), completed)
    self.assertEqual(self.usb.writes, writes)
    self.assertNotIn(0x13, self.usb.commands())
    self.assertNotIn(0x07, self.usb.commands())

  async def test_lost_exposure_blocks_retry(self) -> None:
    """The second CC may have executed; persist partial evidence and never replace it."""
    self.usb.point()
    self.usb.fail_exposure = 2
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(result.record.state, "uncertain")
    self.assertEqual([a.state for a in result.acquisitions], ["complete", "uncertain"])
    self.assertEqual(self.usb.writes.count(b"CC"), 2)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    writes = len(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(len(self.usb.writes), writes)

  async def test_crossed_boundary_preserves_frame_without_accepting_point(self) -> None:
    """A full image is retained but cannot be labeled a successful collection in another cycle."""
    self.usb.point(crossed=True)
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(result.record.state, "uncertain")
    self.assertEqual(result.acquisitions[-1].wire_data, _IMAGE)
    self.assertEqual(result.acquisitions[-1].state, "uncertain")
    self.assertFalse(self.usb.script)

  async def test_missed_window_has_no_exposure(self) -> None:
    """Late polling fails closed before filter movement or CC, and survives a second call."""
    self.usb.observe(_status(remaining=42))
    with self.assertRaisesRegex(ABI7500RunError, "window was missed"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes.count(b"CC"), 0)
    self.assertEqual(await self.driver.get_collections(_RUN_ID), ())
    writes = len(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(len(self.usb.writes), writes)

  async def test_changed_plan_cannot_restart(self) -> None:
    """The run ID owns its collection settings as well as the thermal payload."""
    with self.assertRaisesRegex(ABI7500RunError, "different program or collection plan"):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertEqual(await self.driver.start_run(_RUN_ID, _PROGRAM, collection_plan=_PLAN), _run())
    self.assertFalse(self.usb.writes)

  async def test_optical_start_uses_normal_thermal_upload(self) -> None:
    """The plan stays on the host; only the native thermal method is uploaded once."""
    self.journal.save(replace(_run(), state="completed"))
    self.usb.preflight()
    self.usb.identity()
    self.usb.exchange(0x29, _BASELINE.raw_payload)
    self.usb.optics(0)
    self.usb.exchange(0x29, _BASELINE.raw_payload)
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"ready")
    self.usb.exchange(0x1B)
    self.usb.exchange(0x12, request=_checkpoint("optical-start").wire_program.encode())
    self.usb.exchange(0x73, b"\x00\x02")
    self.usb.exchange(0x29, _BASELINE.raw_payload)
    self.usb.exchange(0x13)
    run = await self.driver.start_run("optical-start", _PROGRAM, collection_plan=_PLAN)
    self.assertEqual(run.state, "starting")
    self.assertEqual(run.collection_plan, _PLAN)
    self.assertEqual(self.usb.commands().count(0x13), 1)
    self.assertFalse(self.usb.script)

  async def test_wait_timeout_persists_failure_without_starting_exposures(self) -> None:
    """A tiny overall deadline cannot turn a waiting run into unjournaled camera activity."""
    self.usb.observe(_status(remaining=60))
    with self.assertRaisesRegex(TimeoutError, "fluorescence observation.*including startup"):
      await self.driver.collect_fluorescence(_RUN_ID, timeout=1e-10)
    run = self.journal.get(_RUN_ID)
    assert run is not None
    self.assertIsNotNone(run.collection_error)
    self.assertEqual(self.usb.writes.count(b"CC"), 0)

  async def test_timeout_after_all_frames_preserves_collections(self) -> None:
    """An expired observation budget cannot erase good frames or authorize a second read."""
    self.usb.observe(_status())
    self.usb.point()
    self.usb.observe(_status(2))
    self.usb.point(2, move=False)
    self.usb.observe(_status(2, remaining=13))
    remaining = self.driver._remaining

    def expire_after_reads(deadline: float, *, context: str = "command") -> float:
      """Expire only the outer observation deadline after both collection commits."""
      second = self.journal.get_collection(CollectionPoint(1, 2, 1).collection_id(_RUN_ID))
      if context.startswith("fluorescence observation") and second is not None:
        if second.state == "complete":
          raise TimeoutError(f"ABI 7500 {context} timed out")
      return remaining(deadline, context=context)

    with patch.object(self.driver, "_remaining", side_effect=expire_after_reads):
      with self.assertRaisesRegex(TimeoutError, "fluorescence observation"):
        await self.driver.collect_fluorescence(_RUN_ID)
    collections = await self.driver.get_collections(_RUN_ID)
    self.assertEqual([c.record.state for c in collections], ["complete", "complete"])
    self.assertEqual(sum(len(c.acquisitions) for c in collections), 6)
    self.assertTrue(all(a.wire_data == _IMAGE for c in collections for a in c.acquisitions))
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "cannot resume"):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)

  async def test_cancelled_scheduler_retains_exposure_intent(self) -> None:
    """Host cancellation after CC records uncertainty and releases the lock for recovery."""
    self.usb.point()
    self.usb.block_exposure = 1
    task = asyncio.create_task(self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1)))
    await self.usb.block_started.wait()
    task.cancel()
    self.usb.unblock.set()
    with self.assertRaises(asyncio.CancelledError):
      await task
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(result.record.state, "uncertain")
    self.assertEqual(result.acquisitions[0].state, "uncertain")
    self.assertIn("CancelledError", result.acquisitions[0].error or "")
    self.assertFalse(self.driver._run_lock.locked())

  async def test_journal_requires_reserved_point(self) -> None:
    """An active thermal run cannot authorize an unrelated snapshot."""
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      self.journal.check_optical_control(_run().camera_description, _run().thermal_version)
    record = CollectionRecord(_RUN_ID, CollectionPoint(1, 1, 1), 1, _status(), ("unrelated",))
    with self.assertRaisesRegex(ABI7500RunError, "does not match"):
      self.journal.reserve_collection(record)
    self.assertFalse(self.usb.writes)
