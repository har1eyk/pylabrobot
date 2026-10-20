"""Exercise six-image collection bursts with scripted USB, not hardware timing claims."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .acquisition import CameraExposure
from .acquisition_tests import _IMAGE
from .camera import CameraGeometry
from .collection import CollectionFilter, CollectionPlan, CollectionPoint
from .collection_tests import _CollectionUSB, _run, _status
from .driver import ABI7500FastDx
from .run import ABI7500RunError
from .run_tests import _RUN_ID
from .status import ABI7500Status
from .thermal_program import ThermalCycle, ThermalProgram, ThermalStep

_FILTERS = (CollectionFilter(2, (0.540, 0.180, 0.060)), CollectionFilter(5, (0.030, 0.010, 0.005)))
_PLAN = CollectionPlan(
  ((0, 0),),
  _FILTERS,
  read_window=25,
  park_between_collections=True,
  home_after_collections=(1, 2, 3),
)


def _hold(repetition: int = 1, remaining: int = 25) -> ABI7500Status:
  """Use the captured controller token with explicit synthetic three-hold indices."""
  data = bytearray(_status(repetition, remaining).raw_payload)
  data[28:30] = b"\0\0"
  return ABI7500Status(bytes(data))


class _DualUSB(_CollectionUSB):
  """Script ordered filter-2/filter-5 reads with optional hold and transport failures."""

  def burst(self, repetition: int = 1, *, failure: str = "") -> None:
    """Countdowns are test inputs, not measured performance estimates."""
    before = _hold(repetition)
    self.observe(before)
    self.optics(0)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")
    for filter_index, setting in enumerate(_FILTERS):
      remaining = 25 - 8 * filter_index
      if filter_index == 1 and failure == "before-filter":
        self.exchange(0x29, _hold(repetition, remaining=3).raw_payload)
        return
      self.exchange(0x29, _hold(repetition, remaining).raw_payload)
      self.exchange(0x69, request=setting.position.to_bytes(2, "big"))
      self.exchange(0x6C, b"\0\1")
      self.exchange(0x6C, b"\0\0")
      position = 2 if filter_index == 1 and failure == "wrong-filter" else setting.position
      self.optics(position)
      if filter_index == 1 and failure == "wrong-filter":
        return
      for index, duration in enumerate(setting.exposures):
        exposure = CameraExposure(duration)
        self.expect_write(exposure.encode())
        self.expect_read(exposure.milliseconds.to_bytes(2, "little"))
        if filter_index == 1 and index == 0 and failure == "before-exposure":
          self.exchange(0x29, _hold(repetition, remaining=2).raw_payload)
          return
        self.exchange(0x29, _hold(repetition, remaining - index * 2).raw_payload)
        self.expect_write(b"CC")
        self.expect_write(b"CH   5")
        self.expect_read(b"ready")
        self.expect_write(CameraGeometry(321, 245).encode_read())
        self.image()
        self.optics(setting.position)
        after = _hold(repetition, remaining - index * 2 - 1)
        if filter_index == 1 and index == 0 and failure == "crossed-hold":
          after = _hold(repetition + 1)
        self.exchange(0x29, after.raw_payload)
        if filter_index == 1 and index == 0 and failure == "crossed-hold":
          return
    self.optics(5)
    self.exchange(0x67)
    self.exchange(0x6C, b"\0\1")
    self.exchange(0x6C, b"\0\0")
    self.optics(0)
    after = _hold(repetition + int(failure == "late-home"), remaining=7)
    self.exchange(0x29, after.raw_payload)


class DualChannelTests(unittest.IsolatedAsyncioTestCase):
  """Preserve identity and partial data while refusing replay across filter boundaries."""

  async def asyncSetUp(self) -> None:
    """Replace USB before connecting and reserve a synthetic three-hold run."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "journal.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _DualUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.driver._geometry = CameraGeometry(321, 245)
    base = _run()
    program = ThermalProgram(
      base.program.metadata,
      (
        ThermalCycle(3, (ThermalStep(60, 30),)),
        ThermalCycle(1, (ThermalStep(30, 60),)),
      ),
    )
    self.driver._require_journal().reserve(
      replace(
        base,
        program=program,
        collection_plan=_PLAN,
        last_status=_hold(),
      )
    )
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Close only the replaced transport and temporary journal."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def test_three_bursts_keep_six_exposures_ordered_and_cache_after_restart(self) -> None:
    """Eighteen camera commands and three homes retain unambiguous filter/exposure identities."""
    for repetition in (1, 2, 3):
      self.usb.burst(repetition)
      point = CollectionPoint(0, repetition, 0)
      await asyncio.gather(
        self.driver._collect_point(_RUN_ID, point), self.driver._collect_point(_RUN_ID, point)
      )
    collections = await self.driver.get_collections(_RUN_ID)
    self.assertEqual([c.record.ordinal for c in collections], [1, 2, 3])
    expected = [(f.position, e) for f in _FILTERS for e in f.exposures]
    for collection in collections:
      self.assertEqual(collection.record.state, "complete")
      self.assertTrue(collection.record.homing_requested)
      self.assertIsNotNone(collection.record.parked_optics)
      self.assertEqual(
        [(a.optics_before.filter_position, a.exposure.duration) for a in collection.acquisitions],
        expected,
      )
      self.assertTrue(
        all(a.state == "complete" and a.wire_data == _IMAGE for a in collection.acquisitions)
      )
      self.assertEqual(
        tuple(a.acquisition_id for a in collection.acquisitions), collection.record.acquisition_ids
      )
    self.assertEqual(self.usb.writes.count(b"CC"), 18)
    self.assertEqual(self.usb.commands().count(0x69), 6)
    self.assertEqual(self.usb.commands().count(0x67), 3)
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    await self.driver._collect_point(_RUN_ID, CollectionPoint(0, 3, 0))
    self.assertEqual(await self.driver.get_collections(_RUN_ID), collections)
    self.assertEqual(self.usb.writes, writes)

  async def failed_burst(self, failure: str, complete: int, uncertain: int) -> None:
    """Check preservation and refusal to resume an incomplete second-filter collection."""
    self.usb.burst(failure=failure)
    point = CollectionPoint(0, 1, 0)
    with self.assertRaises(ABI7500RunError):
      await self.driver._collect_point(_RUN_ID, point)
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(collection.record.state, "uncertain")
    self.assertEqual(sum(a.state == "complete" for a in collection.acquisitions), complete)
    self.assertEqual(sum(a.state == "uncertain" for a in collection.acquisitions), uncertain)
    self.assertEqual(len(collection.record.acquisition_ids), 6)
    self.assertFalse(self.usb.script)
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    with self.assertRaises(ABI7500RunError):
      await self.driver._collect_point(_RUN_ID, point)
    self.assertEqual(self.usb.writes, writes)

  async def test_insufficient_time_before_second_filter(self) -> None:
    """Preserve ROX-channel images without moving or exposing at the green filter."""
    await self.failed_burst("before-filter", complete=3, uncertain=0)
    self.assertEqual(self.usb.commands().count(0x69), 1)
    self.assertEqual(self.usb.writes.count(b"CC"), 3)

  async def test_wrong_second_filter_readback(self) -> None:
    """Do not mislabel images as green when the wheel remains at the reference position."""
    await self.failed_burst("wrong-filter", complete=3, uncertain=0)
    self.assertEqual(self.usb.writes.count(b"CC"), 3)

  async def test_insufficient_time_after_second_filter_move(self) -> None:
    """A successful move does not justify starting an exposure after its time budget expires."""
    await self.failed_burst("before-exposure", complete=3, uncertain=1)
    self.assertEqual(self.usb.writes.count(b"CC"), 3)

  async def test_second_filter_frame_crosses_hold(self) -> None:
    """Retain the complete but uncertain frame and do not collect the remaining exposures."""
    await self.failed_burst("crossed-hold", complete=3, uncertain=1)
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(collection.acquisitions[-1].wire_data, _IMAGE)
    self.assertEqual(self.usb.writes.count(b"CC"), 4)

  async def test_home_crosses_hold(self) -> None:
    """Six valid frames do not make a valid collection when closing optics finishes too late."""
    await self.failed_burst("late-home", complete=6, uncertain=0)
    self.assertEqual(self.usb.commands().count(0x67), 1)

  async def test_lost_second_filter_exposure_is_never_repeated(self) -> None:
    """A lost readiness reply after the fourth CC preserves the first triplet across restart."""
    self.usb.burst()
    self.usb.fail_exposure = 4
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(0, 1, 0))
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(
      [a.state for a in collection.acquisitions], ["complete", "complete", "complete", "uncertain"]
    )
    writes = list(self.usb.writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.driver.io = cast(USB, self.usb)
    with self.assertRaises(ABI7500RunError):
      await self.driver.collect_fluorescence(_RUN_ID)
    self.assertEqual(self.usb.writes, writes)
    self.assertEqual(self.usb.writes.count(b"CC"), 4)
