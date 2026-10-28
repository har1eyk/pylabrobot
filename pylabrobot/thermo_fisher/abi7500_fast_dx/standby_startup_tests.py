"""Verify the captured normal Start with journaled lamp startup and 30 µL controller metadata."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .camera import CameraGeometry
from .collection import CollectionFilter, CollectionPlan
from .collection_tests import _CollectionUSB
from .driver import ABI7500FastDx
from .protocol import ThermalPacket
from .run import ABI7500RunError, ThermalRun
from .run_tests import _BASELINE, _RUN_ID, _checkpoint
from .thermal_program import ThermalProgram

_VECTOR = json.loads((Path(__file__).parent / "test_data/standby_startup_vectors.json").read_text())
_REQUESTS = tuple(
  ThermalPacket.decode(bytes.fromhex(pair["request_hex"])) for pair in _VECTOR["exchanges"]
)
_PROGRAM = ThermalProgram.decode(next(p.payload for p in _REQUESTS if p.command == 0x12))
_TABLE = next(
  ThermalPacket.decode(bytes.fromhex(pair["reply_hex"])).payload
  for pair in _VECTOR["exchanges"]
  if pair["command"] == 0x80
)
_PLAN = CollectionPlan(
  ((1, 0),),
  tuple(
    CollectionFilter(
      position, tuple(ms / 1000 for f, ms in _VECTOR["collection_exposures_ms"] if f == position)
    )
    for position in range(1, 6)
  ),
  read_window=55,
  park_between_collections=True,
  home_after_collections=tuple(range(1, 9)),
)


class StandbyStartupTests(unittest.IsolatedAsyncioTestCase):
  """Byte-exact fake USB cannot enumerate or actuate an instrument."""

  async def asyncSetUp(self) -> None:
    """Install an isolated journal and a strictly ordered transport."""
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
    """Close only the fake transport and temporary journal."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  def preflight(self, power: int = 0) -> None:
    """Establish coherent inactive state, reference geometry and lamp standby."""
    self.usb.preflight()
    self.usb.identity()
    self.usb.exchange(0x29, _BASELINE.raw_payload)
    self.usb.optics(0, power=power)
    self.usb.exchange(0x29, _BASELINE.raw_payload)
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"ready")

  def script_start(self, motor_states: tuple[int, ...] = (0,)) -> None:
    """Check upload/power/table/load/home/normal Start with additional PLR observations."""
    self.preflight()
    usb = self.usb
    usb.exchange(0x1B)
    usb.exchange(0x12, request=replace(_checkpoint(), program=_PROGRAM).wire_program.encode())
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.optics(0, power=0)
    usb.exchange(0x65, request=b"\x00\x01")
    usb.optics(0)
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.exchange(0x80, _TABLE)
    usb.exchange(0x73, b"\x00\x00")
    usb.exchange(0x71)
    usb.exchange(0x73, b"\x00\x02")
    for state in motor_states:
      usb.exchange(0x6C, state.to_bytes(2, "big"))
    usb.optics(0)
    usb.exchange(0x67)
    usb.exchange(0x6C, b"\x00\x01")
    usb.exchange(0x6C, b"\x00\x00")
    usb.optics(0)
    usb.exchange(0x29, _BASELINE.raw_payload)
    usb.exchange(0x13)

  async def start(self) -> ThermalRun:
    """Start the exact normal experiment with explicit standby transition."""
    return await self.driver.start_run(
      _RUN_ID, _PROGRAM, collection_plan=_PLAN, standby_startup=True
    )

  async def test_capture_program_exposures_and_startup_order(self) -> None:
    """Thirty µL is controller metadata; the captured normal Start has no cover override."""
    self.assertEqual(_PROGRAM.metadata[42:], bytes.fromhex("1e00020000"))
    self.assertEqual(
      [(c.repetitions, [(s.temperature, s.hold_time) for s in c.steps]) for c in _PROGRAM.cycles],
      [(1, [(60, 900)]), (8, [(60, 60)]), (1, [(30, 30)])],
    )
    for pair in _VECTOR["exchanges"]:
      request = ThermalPacket.decode(bytes.fromhex(pair["request_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(pair["reply_hex"]))
      self.assertEqual((request.command, request.sequence), (reply.command, reply.sequence))
      self.assertEqual(reply.error_code, 0)
    captured = [p.command for p in _REQUESTS if p.command in (0x1B, 0x12, 0x80, 0x71, 0x67, 0x13)]
    self.assertEqual(captured, [0x1B, 0x12, 0x80, 0x71, 0x67, 0x13])
    self.script_start()
    run = await self.start()
    self.assertFalse(self.usb.script)
    self.assertEqual(
      [c for c in self.usb.commands() if c in (0x1B, 0x12, 0x65, 0x80, 0x71, 0x67, 0x13)],
      [0x1B, 0x12, 0x65, 0x80, 0x71, 0x67, 0x13],
    )
    self.assertNotIn(0x35, self.usb.commands())
    self.assertNotIn(0x7E, self.usb.commands())
    self.assertTrue(run.standby_startup)
    self.assertFalse(run.calibration_startup)
    self.assertIsNone(run.cover_temperature)
    self.assertEqual(ThermalRun.from_dict(run.to_dict()), run)
    self.assertEqual(
      self.driver._require_journal()._open().execute("SELECT version FROM format").fetchall(),
      [(6,)],
    )
    writes = list(self.usb.writes)
    self.assertEqual(await self.start(), run)
    self.assertEqual(self.usb.writes, writes)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.assertEqual(await self.start(), run)
    with self.assertRaises(ABI7500RunError):
      await self.driver.start_run(
        _RUN_ID, replace(_PROGRAM, metadata=_checkpoint().program.metadata), collection_plan=_PLAN
      )

  async def test_lost_home_acknowledgement_never_replays(self) -> None:
    """An ambiguous preparatory home cannot lead to Start or a second home."""
    self.script_start()
    self.usb.fail_command = 0x67
    with self.assertRaises(TimeoutError):
      await self.start()
    writes = list(self.usb.writes)
    self.assertEqual((await self.start()).state, "not_started")
    self.assertEqual(self.usb.writes, writes)
    self.assertNotIn(0x13, self.usb.commands())
    self.assertEqual(self.usb.commands().count(0x67), 1)

  async def test_normal_start_waits_for_motor_after_loading(self) -> None:
    """Normal standby startup also waits for stationary optics before its single home."""
    self.script_start(motor_states=(1, 0))
    run = await self.start()
    self.assertTrue(run.start_acknowledged)
    self.assertFalse(self.usb.script)
    commands = self.usb.commands()
    self.assertEqual(commands.count(0x71), 1)
    self.assertEqual(commands.count(0x67), 1)
    self.assertEqual(commands.count(0x13), 1)

  async def test_lost_start_acknowledgement_remains_uncertain(self) -> None:
    """Reopening the journal does not repeat an ambiguous normal Start."""
    self.script_start()
    self.usb.fail_command = 0x13
    with self.assertRaises(TimeoutError):
      await self.start()
    self.assertEqual(self.usb.commands().count(0x13), 1)
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    run = await self.start()
    self.assertEqual(run.state, "uncertain")
    self.assertTrue(run.standby_startup)
    self.assertFalse(run.start_acknowledged)

  async def test_mode_and_standby_are_required_before_actuation(self) -> None:
    """Reject incompatible flags or unsupported metadata without silently choosing a mode."""
    for arguments in (
      {"standby_startup": 1, "collection_plan": _PLAN},
      {"standby_startup": True},
      {"standby_startup": True, "collection_plan": _PLAN, "cover_temperature": 90},
      {
        "standby_startup": True,
        "calibration_startup": True,
        "collection_plan": _PLAN,
        "cover_temperature": 90,
      },
      {},
    ):
      with self.subTest(arguments=arguments), self.assertRaises(ValueError):
        await self.driver.start_run(_RUN_ID, _PROGRAM, **arguments)
    self.assertFalse(self.usb.writes)
    self.preflight(power=1)
    with self.assertRaisesRegex(ABI7500RunError, "standby"):
      await self.start()
    self.assertFalse(self.usb.script)
    self.assertNotIn(0x12, self.usb.commands())

  async def test_checkpoint_mode_validation_and_legacy_defaults(self) -> None:
    """Old records keep their mode; incompatible saved startup settings cannot be loaded."""
    legacy = _checkpoint().to_dict()
    legacy.pop("standby_startup")
    self.assertFalse(ThermalRun.from_dict(legacy).standby_startup)
    valid = replace(_checkpoint(), program=_PROGRAM, collection_plan=_PLAN, standby_startup=True)
    for changes in (
      {"standby_startup": 1},
      {"collection_plan": None},
      {"cover_temperature": 90},
      {"start_command": 0x7E, "cover_temperature": 90},
      {"calibration_startup": True, "cover_temperature": 90, "start_command": 0x7E},
    ):
      with self.subTest(changes=changes), self.assertRaises(ValueError):
        ThermalRun.from_dict(valid.to_dict() | changes)
