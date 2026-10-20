"""Captured calibration startup, immutable processing caches, and interrupted motion."""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from ._run_journal import RunJournal
from .acquisition import CameraAcquisition, CameraExposure, OpticalState
from .calibration_exposure import CalibrationExposureProbe
from .calibration_exposure_tests import _FLAT, _MASK, _PLAN
from .camera import CameraFrame, CameraGeometry
from .collection_tests import _CollectionUSB
from .driver import ABI7500FastDx
from .fluorescence import FluorescenceFrame
from .image_processing import PlatePixelMask
from .protocol import ThermalPacket
from .run import ABI7500RunError, ThermalRun
from .run_tests import _BASELINE, _RUN_ID, _checkpoint
from .status import ABI7500Status
from .thermal_program import ThermalProgram

_VECTOR = json.loads(
  (Path(__file__).parent / "test_data/calibration_sequence_vectors.json").read_text()
)
_PACKETS = tuple(ThermalPacket.decode(bytes.fromhex(p["request_hex"])) for p in _VECTOR["startup"])
_PROGRAM = ThermalProgram.decode(next(p.payload for p in _PACKETS if p.command == 0x12))
_COLLECTION = replace(_PLAN, steps=((0, 0),), read_window=252, park_after_exposure_search=True)
_COVER = bytes.fromhex("005a00010069")


class CalibrationSequenceEvidenceTests(unittest.TestCase):
  """Keep captured method settings and immutable cache keys independent of device access."""

  def test_capture_order_and_closed_transition(self) -> None:
    """The capture pins the 30 µL/312-second method and the intervening absolute park."""
    self.assertEqual(_PROGRAM.metadata[42:], bytes.fromhex("1e00020000"))
    self.assertEqual(_PROGRAM.cycles[0].steps[0].temperature, 60)
    self.assertEqual(_PROGRAM.cycles[0].steps[0].hold_time, 312)
    self.assertEqual(
      [
        ABI7500Status(
          ThermalPacket.decode(bytes.fromhex(p["reply_hex"])).payload
        ).remaining_hold_time
        for p in _VECTOR["trigger_status"]
      ],
      [253, 251],
    )
    timing = _VECTOR["timing"]
    self.assertEqual(
      _COLLECTION.read_window,
      (timing["native_estimated_read_ms"] + timing["poll_delta_ms"]) // 1000,
    )
    self.assertEqual(
      [p.command for p in _PACKETS],
      [0x35, 0x1B, 0x12, 0x64, 0x65, 0x64, 0x65, 0x80, 0x71, 0x67, 0x7E],
    )
    filters = [
      ThermalPacket.decode(bytes.fromhex(p["request_hex"])) for p in _VECTOR["filter_commands"]
    ]
    self.assertEqual(
      [int.from_bytes(p.payload, "big") for p in filters if p.command == 0x69],
      [1, 2, 3, 4, 5, 0, 1, 2, 3, 4, 5, 0],
    )
    for item in _VECTOR["startup"] + _VECTOR["filter_commands"]:
      request = ThermalPacket.decode(bytes.fromhex(item["request_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(item["reply_hex"]))
      self.assertEqual((request.command, request.sequence), (reply.command, reply.sequence))
      self.assertEqual(reply.error_code, 0)

  def test_cached_probe_is_bound_to_original_frame_and_mask(self) -> None:
    """Equal immutable inputs reuse computation; altered camera pixels cannot reuse counts."""
    source = CameraFrame(CameraGeometry(321, 245), _FLAT)
    first = FluorescenceFrame(source, CameraExposure(2), _MASK)
    same_mask = PlatePixelMask(_MASK.source_bytes, _MASK.filter)
    second = FluorescenceFrame(source, CameraExposure(2), same_mask)
    self.assertIs(_MASK.wells, same_mask.wells)
    self.assertIs(
      CalibrationExposureProbe.from_frame(first), CalibrationExposureProbe.from_frame(second)
    )
    bright = CameraFrame(source.geometry, b"\xff\x0f" * 78645 + bytes(22))
    changed = CalibrationExposureProbe.from_frame(
      FluorescenceFrame(bright, CameraExposure(2), same_mask)
    )
    self.assertEqual(max(CalibrationExposureProbe.from_frame(first).saturation_counts), 0)
    self.assertGreater(max(changed.saturation_counts), 6)

  def test_owner_cache_detects_external_changes_and_local_owner_changes(self) -> None:
    """External corruption and locally saved identity changes invalidate the historical audit."""
    run = _checkpoint()
    acquisition = CameraAcquisition(
      "owner-cache",
      CameraExposure(0.256),
      run.camera_description,
      run.thermal_version,
      CameraGeometry(321, 245),
      "2026-10-20T00:00:00+00:00",
      _BASELINE,
      OpticalState(b"\0\0", b"\0\0", b"\0\0\0\2"),
    )
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "journal.sqlite3"
      journal = RunJournal(path)
      try:
        journal.reserve_acquisition(acquisition)
        journal.check_optical_control(run.camera_description, run.thermal_version)
        corrupted = acquisition.to_dict() | {"acquisition_id": "wrong-primary-key"}
        with sqlite3.connect(path) as external:
          external.execute("UPDATE acquisitions SET record=?", (json.dumps(corrupted),))
        with self.assertRaisesRegex(ABI7500RunError, "identity mismatch"):
          journal.check_optical_control(run.camera_description, run.thermal_version)
        journal.save_acquisition(acquisition)
        journal.check_optical_control(run.camera_description, run.thermal_version)
        journal.save_acquisition(replace(acquisition, camera_description="different camera"))
        with self.assertRaisesRegex(ABI7500RunError, "different instrument"):
          journal.check_optical_control(run.camera_description, run.thermal_version)
        journal.save_acquisition(acquisition)
        journal.check_optical_control(run.camera_description, run.thermal_version)
        journal.reserve(run)
        with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
          journal.check_optical_control(run.camera_description, run.thermal_version)
      finally:
        journal.close()


class CalibrationStartupTests(unittest.IsolatedAsyncioTestCase):
  """Use strict fake USB to check the entire idle-to-Start transition and failure recovery."""

  async def asyncSetUp(self) -> None:
    """Install an isolated journal and transport before any setup or device enumeration."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _CollectionUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.driver._geometry = CameraGeometry(321, 245)
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Release only the fake transport and isolated journal."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  def preflight(self, power: int = 0) -> None:
    """Script coherent inactivity and calibration standby behind filter zero."""
    usb = self.usb
    usb.preflight()
    usb.identity()
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.optics(0, power=power)
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.expect_write(b"CH   5")
    usb.expect_read(b"ready")

  def script_start(self) -> None:
    """Follow captured mutation order, retaining PLR's additional state verification."""
    usb = self.usb
    self.preflight()
    usb.exchange(0x36, bytes.fromhex("006900010069"))
    usb.exchange(0x35, request=bytes.fromhex("005a0001"))
    usb.exchange(0x36, _COVER)
    usb.exchange(0x1B)
    run = replace(_checkpoint(), program=_PROGRAM)
    usb.exchange(0x12, request=run.wire_program.encode())
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.optics(0, power=0)
    usb.exchange(0x65, request=b"\x00\x01")
    usb.optics(0)
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.exchange(0x80, bytes(254))
    usb.exchange(0x73, b"\x00\x00")
    usb.exchange(0x71)
    usb.exchange(0x73, b"\x00\x02")
    usb.optics(0)
    usb.exchange(0x67)
    usb.exchange(0x6C, b"\x00\x01")
    usb.exchange(0x6C, b"\x00\x00")
    usb.optics(0)
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.exchange(0x36, _COVER)
    usb.exchange(0x7E)
    usb.exchange(0x36, _COVER)

  async def start(self) -> ThermalRun:
    """Submit the explicit calibration mode with its journaled collection intent."""
    return await self.driver.start_run(
      _RUN_ID,
      _PROGRAM,
      collection_plan=_COLLECTION,
      cover_temperature=90,
      calibration_startup=True,
    )

  async def test_startup_order_and_restart_are_idempotent(self) -> None:
    """Upload, lamp power, load, home and Start occur once; reopening cannot replay them."""
    self.script_start()
    run = await self.start()
    self.assertFalse(self.usb.script)
    mutations = [c for c in self.usb.commands() if c in (0x35, 0x1B, 0x12, 0x65, 0x71, 0x67, 0x7E)]
    self.assertEqual(mutations, [0x35, 0x1B, 0x12, 0x65, 0x71, 0x67, 0x7E])
    self.assertTrue(run.calibration_startup)
    self.assertEqual(ThermalRun.from_dict(run.to_dict()), run)
    self.assertEqual(
      self.driver._require_journal()._open().execute("SELECT version FROM format").fetchall(),
      [(5,)],
    )
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.assertEqual(await self.start(), run)
    with self.assertRaises(ValueError):
      await self.driver.start_run(
        _RUN_ID, _PROGRAM, collection_plan=_COLLECTION, cover_temperature=90
      )

  async def test_ambiguous_home_does_not_start_or_repeat(self) -> None:
    """An uncertain homing reply permanently blocks replay for this run identity."""
    self.script_start()
    self.usb.fail_command = 0x67
    with self.assertRaises(TimeoutError):
      await self.start()
    writes = list(self.usb.writes)
    self.assertEqual((await self.start()).state, "not_started")
    self.assertEqual(self.usb.writes, writes)
    self.assertEqual(self.usb.commands().count(0x67), 1)
    self.assertNotIn(0x7E, self.usb.commands())

  async def test_standby_and_explicit_mode_are_required(self) -> None:
    """A 30 µL method is not silently accepted as ordinary PCR or with lamp power one."""
    with self.assertRaises(ValueError):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertFalse(self.usb.writes)
    self.preflight(power=1)
    with self.assertRaisesRegex(ABI7500RunError, "standby"):
      await self.start()
    self.assertFalse(self.usb.script)
    self.assertNotIn(0x12, self.usb.commands())
