"""Captured exposure-search decisions and fault-injected calibration acquisition, offline only."""

import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .calibration_exposure import CalibrationExposureProbe, CalibrationExposureSearch
from .camera import CameraGeometry
from .collection import CollectionFilter, CollectionPlan, CollectionPoint
from .collection_tests import _CollectionUSB, _run, _status
from .driver import ABI7500FastDx
from .fluorescence_tests import _mask
from .protocol import ThermalPacket
from .run import ABI7500RunError, ThermalRun
from .run_tests import _BASELINE, _PROGRAM, _RUN_ID, _checkpoint, _RunUSB
from .status import ABI7500CoverState

_VECTORS = json.loads(
  (Path(__file__).parent / "test_data/calibration_exposure_vectors.json").read_text()
)
_MASK = _mask(
  {"mask_pixels": [[r, c] for r in range(10, 13) for c in range(10, 13)], "physical_filter": 1}
)
_PLAN = CollectionPlan(
  ((1, 1),), (CollectionFilter(1, (2.0,), "automatic"),), exposure_search_masks=(_MASK,)
)
_FLAT = struct.pack("<78645H", *([128] * 78645)) + bytes(22)


_START_VECTORS = json.loads(
  (Path(__file__).parent / "test_data/calibration_start_vectors.json").read_text()
)


def _script_calibration_start(usb: _RunUSB, *, after: bytes = b"\x00\x5a\x00\x01\x00\x69") -> None:
  """Use captured command ordering, with synthetic 90-degree configuration readbacks."""
  usb.preflight()
  usb.exchange(0x36, b"\x00\x69\x00\x01\x00\x69")
  usb.exchange(0x35, request=b"\x00\x5a\x00\x01")
  usb.exchange(0x36, b"\x00\x5a\x00\x01\x00\x69")
  usb.exchange(0x1B)
  usb.exchange(0x12, request=_checkpoint().wire_program.encode())
  usb.exchange(0x73, b"\x00\x02")
  usb.exchange(0x29, _BASELINE.raw_payload)
  usb.exchange(0x36, b"\x00\x5a\x00\x01\x00\x69")
  usb.exchange(0x7E)
  usb.exchange(0x36, after)


class CalibrationExposureTests(unittest.TestCase):
  """Keep the vendor's integer boundaries and per-well saturation rule exact."""

  def test_captured_cover_preserving_start_and_failed_readbacks(self) -> None:
    """Vendor calibration starts with 0x7E; the failed PLR run used 0x13 and read back 105."""
    commands = []
    for pair in _START_VECTORS["vendor_cover_start_pairs"]:
      request = ThermalPacket.decode(bytes.fromhex(pair["request"]["packet_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(pair["reply"]["packet_hex"]))
      self.assertEqual((request.command, request.sequence), (reply.command, reply.sequence))
      self.assertEqual((reply.error_code, reply.payload), (0, b""))
      commands.append(request.command)
      if request.command == 0x7E:
        self.assertEqual(request.payload, b"")
    self.assertEqual(commands, [0x35, 0x1B, 0x12, 0x71, 0x7E])
    readbacks = []
    for pair in _START_VECTORS["plr_cover_pairs"]:
      request = ThermalPacket.decode(bytes.fromhex(pair["request_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(pair["reply_hex"]))
      self.assertEqual(reply.error_code, 0)
      if request.command == 0x36:
        cover = ABI7500CoverState(reply.payload)
        readbacks.append((cover.setpoint, cover.mode, cover.measured_raw))
    self.assertEqual(readbacks, [(105, 1, 104), (105, 1, 105), (105, 1, 105)])

  def test_all_captured_searches(self) -> None:
    """All 35 vendor probes and final five settings replay without fitting parameters."""
    results = []
    for item in _VECTORS["filters"]:
      search = CalibrationExposureSearch()
      for probe in item["probes"]:
        self.assertEqual(search.next_milliseconds, probe["milliseconds"])
        search = search.add(
          CalibrationExposureProbe(probe["milliseconds"], tuple(probe["saturation_counts"]))
        )
      self.assertIsNone(search.next_milliseconds)
      self.assertEqual(search.selected_milliseconds, item["selected_milliseconds"])
      results.append(search.selected_milliseconds)
    self.assertEqual(results, [1800, 720, 1719, 1800, 558])

  def test_boundaries_and_invalid_history(self) -> None:
    """Six bright pixels in every well is safe; seven in even one well starts bisection."""
    safe = CalibrationExposureProbe(2000, (6,) * 96)
    saturated = CalibrationExposureProbe(2000, (7,) + (0,) * 95)
    self.assertEqual(CalibrationExposureSearch((safe,)).selected_milliseconds, 1800)
    self.assertEqual(CalibrationExposureSearch((saturated,)).next_milliseconds, 1005)
    for probes in ((safe, safe), (CalibrationExposureProbe(1999, (0,) * 96),)):
      with self.assertRaisesRegex(ValueError, "history"):
        CalibrationExposureSearch(probes)
    search = CalibrationExposureSearch()
    with self.assertRaisesRegex(ValueError, "No usable unsaturated"):
      for _ in range(11):
        ms = search.next_milliseconds
        assert ms is not None
        search = search.add(CalibrationExposureProbe(ms, (7,) * 96))

  def test_masks_and_budgets_are_part_of_identity(self) -> None:
    """Serialized calibration plans pin ROI bytes and reserve the worst-case probe budget."""
    self.assertEqual(CollectionPlan.from_dict(_PLAN.to_dict()), _PLAN)
    self.assertEqual(len(_PLAN.probe_ids("test")), 11)
    with self.assertRaisesRegex(ValueError, "matching masks"):
      replace(_PLAN, exposure_search_masks=())
    with self.assertRaisesRegex(ValueError, "cannot fit"):
      replace(_PLAN, read_window=25)
    with self.assertRaisesRegex(ValueError, "2-second"):
      CollectionFilter(1, (1.0,), "automatic")

  def test_cover_framing_and_measured_guard(self) -> None:
    """The captured set request uses 90 integer degrees; the readback model keeps raw units."""
    pair = _VECTORS["cover_set_exchange"]
    request = ThermalPacket.decode(bytes.fromhex(pair["request"]["packet_hex"]))
    reply = ThermalPacket.decode(bytes.fromhex(pair["reply"]["packet_hex"]))
    self.assertEqual((request.command, request.payload), (0x35, b"\x00\x5a\x00\x01"))
    self.assertEqual((reply.command, reply.payload, reply.error_code), (0x35, b"", 0))
    state = ABI7500CoverState(b"\x00\x5a\x00\x01\x03\x84")
    self.assertEqual((state.setpoint, state.mode, state.measured_raw), (90, 1, 900))
    run = replace(_run(), cover_temperature=90, cover_configured=True)
    self.assertEqual(ThermalRun.from_dict(run.to_dict()), run)
    with self.assertRaisesRegex(ABI7500RunError, "cover temperature"):
      ABI7500FastDx._check_collection_cover(run, _status())
    with self.assertRaises(ValueError):
      replace(run, cover_temperature=900)


class CalibrationAcquisitionTests(unittest.IsolatedAsyncioTestCase):
  """Exercise exact transport ordering, partial probes and durable restart prevention."""

  async def asyncSetUp(self) -> None:
    """Bind a transport fake and fresh journal; never enumerate or open a real device."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "run.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _CollectionUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.driver._geometry = CameraGeometry(321, 245)

  async def asyncTearDown(self) -> None:
    """Close only the fake transport and temporary journal."""
    await self.driver.stop()
    self.temp.cleanup()

  def script_collection(self, *, bisect: bool = False, park_search: bool = False) -> None:
    """Replay vendor search decisions using synthetic bright/dim images, then one final image."""
    journal = self.driver._require_journal()
    journal.reserve(
      replace(_run(), collection_plan=replace(_PLAN, park_after_exposure_search=park_search))
    )
    before = _status()
    self.usb.observe(before)
    self.usb.optics(0)
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"ready")
    probes = _VECTORS["filters"][1 if bisect else 0]["probes"]
    exposures = [(p["milliseconds"], max(p["saturation_counts"]) > 6) for p in probes]
    exposures.append((_VECTORS["filters"][1 if bisect else 0]["selected_milliseconds"], False))
    for index, (ms, saturated) in enumerate(exposures):
      if park_search and index == len(probes):
        self.usb.optics(1)
        self.usb.exchange(0x69, request=b"\x00\x00")
        self.usb.exchange(0x6C, b"\x00\x00")
        self.usb.optics(0)
      if index in (0, len(probes)):
        self.usb.exchange(0x29, before.raw_payload)
        if index == 0 or park_search:
          self.usb.exchange(0x69, request=b"\x00\x01")
          self.usb.exchange(0x6C, b"\x00\x00")
        self.usb.optics(1)
      self.usb.expect_write(f"CJ   2, {ms:4d}".encode("ascii"))
      self.usb.expect_read(ms.to_bytes(2, "little"))
      self.usb.exchange(0x29, before.raw_payload)
      self.usb.expect_write(b"CC")
      self.usb.expect_write(b"CH   5")
      self.usb.expect_read(b"ready")
      self.usb.expect_write(CameraGeometry(321, 245).encode_read())
      wire = struct.pack("<78645H", *([4095] * 78645)) + bytes(22) if saturated else _FLAT
      for offset in range(0, len(wire), 4096):
        part = wire[offset : offset + 4096]
        self.usb.expect_read(part, len(part))
      self.usb.optics(1)
      self.usb.exchange(0x29, before.raw_payload)
    self.usb.exchange(0x29, before.raw_payload)

  async def test_search_closes_light_path_before_final_images(self) -> None:
    """The explicit plan moves 1→0→1 between the probe and final image without homing."""
    self.script_collection(park_search=True)
    await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertFalse(self.usb.script)
    self.assertEqual(self.usb.commands().count(0x69), 3)
    self.assertNotIn(0x67, self.usb.commands())
    self.assertEqual(self.usb.writes.count(b"CC"), 2)

  async def test_failed_search_park_blocks_final_images_and_replay(self) -> None:
    """An ambiguous shutter transition preserves probes and never exposes a final image."""
    self.script_collection(park_search=True)
    with patch.object(self.driver, "_park_collection_filter", AsyncMock(side_effect=TimeoutError)):
      with self.assertRaises(TimeoutError):
        await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    writes = list(self.usb.writes)
    with self.assertRaises(ABI7500RunError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertEqual(self.usb.writes, writes)

  async def test_probe_and_final_images_are_separate_and_persistent(self) -> None:
    """Final-image processing sees only reduced exposures, while original probes survive restart."""
    self.script_collection()
    await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(result.record.state, "complete")
    self.assertEqual([a.exposure.milliseconds for a in result.acquisitions], [1800])
    self.assertEqual([a.exposure.milliseconds for a in result.exposure_probes], [2000])
    self.assertEqual(self.usb.writes.count(b"CC"), 2)
    self.assertFalse(self.usb.script)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.assertEqual((await self.driver.get_collections(_RUN_ID))[0], result)
    with self.assertRaisesRegex(ABI7500RunError, "different program"):
      await self.driver.start_run(_RUN_ID, _PROGRAM)

  async def test_ambiguous_probe_is_not_repeated(self) -> None:
    """A lost CC acknowledgement retains the reserved probe and prevents any second capture."""
    self.script_collection()
    self.usb.fail_exposure = 1
    with self.assertRaises(TimeoutError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(result.record.state, "uncertain")
    self.assertFalse(result.acquisitions)
    self.assertEqual(result.exposure_probes[0].state, "uncertain")
    writes = list(self.usb.writes)
    with self.assertRaises(ABI7500RunError):
      await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    self.assertEqual(self.usb.writes, writes)

  async def test_full_bisection_keeps_saturated_probes_out_of_final_images(self) -> None:
    """Eleven probe images can saturate without becoming final calibration data."""
    self.script_collection(bisect=True)
    await self.driver._collect_point(_RUN_ID, CollectionPoint(1, 1, 1))
    result = (await self.driver.get_collections(_RUN_ID))[0]
    self.assertEqual(len(result.exposure_probes), 11)
    self.assertEqual([a.exposure.milliseconds for a in result.acquisitions], [720])
    self.assertEqual(self.usb.writes.count(b"CC"), 12)
    self.assertFalse(self.usb.script)

  async def test_unneeded_cover_write_is_omitted(self) -> None:
    """An already matching cover setting requires only a query."""
    self.usb.exchange(0x36, b"\x00\x5a\x00\x01\x03\x84")
    await self.driver._configure_calibration_cover()
    self.assertEqual(self.usb.commands(), [0x36])

  async def test_lost_cover_acknowledgement_is_not_retried(self) -> None:
    """A transport fault after the cover write blocks collection and survives restart."""
    usb = _RunUSB()
    self.driver.io = cast(USB, usb)
    _script_calibration_start(usb)
    usb.fail_command = 0x35
    with self.assertRaises(TimeoutError):
      await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    writes = list(usb.writes)
    run = await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    self.assertFalse(run.cover_configured)
    self.assertEqual(run.state, "not_started")
    self.assertNotIn(0x7E, usb.commands())
    self.assertNotIn(0x13, usb.commands())
    with self.assertRaisesRegex(ABI7500RunError, "not verified"):
      self.driver._check_collection_cover(run, _status())
    self.assertEqual(usb.writes, writes)

  async def test_cover_configuration_and_duplicate_run(self) -> None:
    """Set cover before preparation, use the preserving Start, and never repeat either write."""
    usb = _RunUSB()
    self.driver.io = cast(USB, usb)
    _script_calibration_start(usb)
    run = await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    self.assertTrue(run.start_acknowledged)
    self.assertTrue(run.cover_configured)
    self.assertEqual(run.start_command, 0x7E)
    self.assertEqual(usb.commands().count(0x7E), 1)
    self.assertNotIn(0x13, usb.commands())
    self.assertFalse(usb.script)
    writes = list(usb.writes)
    self.assertEqual(await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90), run)
    with self.assertRaisesRegex(ABI7500RunError, "cover setting"):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertEqual(usb.writes, writes)
    self.assertEqual(
      self.driver._require_journal()._open().execute("SELECT version FROM format").fetchall(),
      [(4,)],
    )

  async def test_cover_mismatch_prevents_start(self) -> None:
    """Failure to establish 90 degrees while idle cannot submit a program or either Start."""
    usb = _RunUSB()
    self.driver.io = cast(USB, usb)
    usb.preflight()
    usb.exchange(0x36, b"\x00\x69\x00\x01\x00\x69")
    usb.exchange(0x35, request=b"\x00\x5a\x00\x01")
    usb.exchange(0x36, b"\x00\x69\x00\x01\x00\x69")
    with self.assertRaisesRegex(ABI7500RunError, "readback"):
      await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    run = await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    self.assertEqual(run.state, "not_started")
    self.assertFalse(run.start_acknowledged)
    self.assertTrue(all(c not in usb.commands() for c in (0x12, 0x13, 0x7E)))
    self.assertFalse(usb.script)

  async def test_lost_preserving_start_has_no_standard_start_fallback(self) -> None:
    """An ambiguous calibration Start is durable and cannot fall back to another Start mode."""
    usb = _RunUSB()
    self.driver.io = cast(USB, usb)
    _script_calibration_start(usb)
    usb.fail_command = 0x7E
    with self.assertRaises(TimeoutError):
      await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    writes = list(usb.writes)
    run = await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    self.assertEqual(run.state, "uncertain")
    self.assertFalse(run.start_acknowledged)
    self.assertEqual(run.start_command, 0x7E)
    self.assertEqual(usb.commands().count(0x7E), 1)
    self.assertNotIn(0x13, usb.commands())
    self.assertEqual(usb.writes, writes)

  async def test_legacy_cover_run_retains_its_actual_start_command(self) -> None:
    """A saved failed 0x13 run is never relabeled or restarted as a preserving 0x7E run."""
    legacy = replace(_run(), cover_temperature=90, state="cancelled").to_dict()
    del legacy["start_command"]
    run = ThermalRun.from_dict(legacy)
    self.assertEqual(run.start_command, 0x13)
    self.driver._require_journal().reserve(run)
    with self.assertRaisesRegex(ABI7500RunError, "Start command"):
      await self.driver.start_run(
        _RUN_ID, run.program, collection_plan=run.collection_plan, cover_temperature=90
      )
    self.assertFalse(self.usb.writes)
    self.assertEqual(
      self.driver._require_journal()._open().execute("SELECT version FROM format").fetchall(),
      [(3,)],
    )

  async def test_cover_readback_mismatch_preserves_started_run(self) -> None:
    """Bad readback after Start leaves an uncertain owned run; the driver never resends Start."""
    usb = _RunUSB()
    self.driver.io = cast(USB, usb)
    _script_calibration_start(usb, after=b"\x00\x69\x00\x01\x00\x69")
    with self.assertRaisesRegex(ABI7500RunError, "readback"):
      await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    writes = list(usb.writes)
    run = await self.driver.start_run(_RUN_ID, _PROGRAM, cover_temperature=90)
    self.assertEqual(run.state, "uncertain")
    self.assertTrue(run.start_acknowledged)
    self.assertEqual(usb.writes, writes)
