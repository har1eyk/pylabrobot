"""Captured plate traffic and durable release guards, using only isolated journals and fake USB."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from ._run_journal import RunJournal
from .driver import ABI7500DeviceError, ABI7500FastDx
from .protocol import ABI7500ProtocolError, ThermalPacket, encode_serial_read, encode_serial_write
from .run import ABI7500History, ABI7500RunError, ThermalRun
from .run_tests import _RUN_ID, _checkpoint, _history, _payload, _running, _RunUSB
from .status import ABI7500PlateStatus, ABI7500Status

_FIXTURE = json.loads((Path(__file__).parent / "test_data/plate_release_vectors.json").read_text())
_IDLE = _payload("acquire", "inactive_status")
_HISTORY = _history("acquire", _RUN_ID)
_REAL_SLEEP = asyncio.sleep


class _StateChecks(TypedDict, total=False):
  """Typed overrides for one captured preflight script."""

  status: bytes
  history: bytes
  position: int
  motor: int
  lamp: bytes
  after: bytes


def _finished() -> ThermalRun:
  """Create a completed checkpoint from recorded status/history and an explicit synthetic tag."""
  return replace(
    _running("acquire"),
    state="completed",
    last_status=ABI7500Status(_IDLE),
    history=ABI7500History(_HISTORY),
  )


class _PlateUSB(_RunUSB):
  """Script plate preflight and motion without replacing driver decisions or journal writes."""

  def state_checks(
    self,
    *,
    status: bytes = _IDLE,
    history: bytes = _HISTORY,
    position: int = 0,
    motor: int = 0,
    lamp: bytes = b"\x00\x00\x00\x02",
    after: bytes = _IDLE,
  ) -> None:
    """Queue current inactivity, terminal history, optics and another thermal observation."""
    self.exchange(0x29, status)
    self.exchange(0x19, history)
    self.exchange(0x6C, motor.to_bytes(2, "big"))
    self.exchange(0x6B, position.to_bytes(2, "big"))
    self.exchange(0x66, lamp)
    self.exchange(0x29, after)

  def release_preflight(self, plate: int = 2) -> None:
    """Queue the identity/state checks before considering release."""
    self.identity()
    self.state_checks()
    self.exchange(0x73, plate.to_bytes(2, "big"))
    if plate == 2:
      self.exchange(0x29, _IDLE)

  def release_poll(self, plate: int, status: bytes = _IDLE) -> None:
    """Queue one post-command plate observation and matching inactivity check."""
    self.exchange(0x73, plate.to_bytes(2, "big"))
    self.exchange(0x29, status)


class PlateStatusTests(unittest.TestCase):
  """The response parser preserves native codes and rejects malformed checkpoints."""

  def test_status_values_and_payload_validation(self) -> None:
    """Unknown does not alias unloaded, including a value with a nonzero high byte."""
    for code, expected in ((0, "unloaded"), (1, "moving"), (2, "loaded"), (256, "unknown")):
      status = ABI7500PlateStatus(code.to_bytes(2, "big"))
      self.assertEqual((status.state_code, status.state), (code, expected))
    for data in (b"", b"\x00", b"\x00\x00\x00", bytearray(2)):
      with self.assertRaises(ABI7500ProtocolError):
        ABI7500PlateStatus(data)  # type: ignore[arg-type]

  def test_checkpoint_compatibility_and_format_upgrade(self) -> None:
    """Legacy reads preserve the journal; release intent upgrades it atomically for old readers."""
    legacy = _finished().to_dict()
    del legacy["plate_release_state"]
    self.assertEqual(ThermalRun.from_dict(legacy).plate_release_state, "not_requested")
    with self.assertRaises(ValueError):
      ThermalRun.from_dict({**legacy, "plate_release_state": "retry"})
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "runs.sqlite3"
      with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE format (version INTEGER NOT NULL)")
        db.execute("INSERT INTO format VALUES (1)")
        db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        db.execute("INSERT INTO runs VALUES (?, ?)", (_RUN_ID, json.dumps(legacy)))
      journal = RunJournal(path)
      try:
        self.assertEqual(journal.get(_RUN_ID), _finished())
        with sqlite3.connect(path) as db:
          self.assertEqual(db.execute("SELECT version FROM format").fetchall(), [(1,)])
        pending = replace(_finished(), plate_release_state="uncertain")
        journal.save(pending)
        with sqlite3.connect(path) as db:
          self.assertEqual(db.execute("SELECT version FROM format").fetchall(), [(2,)])
        journal.close()
        self.assertEqual(journal.get(_RUN_ID), pending)
      finally:
        journal.close()


class PlateDriverTests(unittest.IsolatedAsyncioTestCase):
  """Release must be guarded, single-dispatch and recoverable through observation alone."""

  async def asyncSetUp(self) -> None:
    """Replace USB before any operation and use a disposable local journal."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _PlateUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.journal = self.driver._require_journal()
    self.journal.reserve(_finished())
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Close only the fake USB and disposable journal."""
    self.usb.unblock.set()
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def restart(self) -> None:
    """Create another controller over the same journal, replacing its USB before use."""
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _PlateUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.journal = self.driver._require_journal()

  def assert_release_state(self, expected: str) -> None:
    """Check the on-disk checkpoint, not a transient driver attribute."""
    saved = self.journal.get(_RUN_ID)
    assert saved is not None
    self.assertEqual(saved.plate_release_state, expected)

  async def test_original_packet_and_usb_replay(self) -> None:
    """Replay native release packets and the actual fragmented Ubuntu status/release transfers."""
    for session in _FIXTURE["sessions"]:
      for exchange in session["exchanges"]:
        request = ThermalPacket.decode(bytes.fromhex(exchange["request_hex"]))
        reply = ThermalPacket.decode(bytes.fromhex(exchange["reply_hex"]))
        self.assertEqual(request.command, exchange["command"])
        self.assertEqual((request.payload, reply.error_code), (b"", 0))
        self.assertEqual(request.sequence, reply.sequence)
        self.assertEqual(reply.command, request.command)
        self.driver._sequence = request.sequence - 1
        if "usb_operations" in exchange:
          for operation in exchange["usb_operations"]:
            wire = bytes.fromhex(operation["data_hex"])
            if operation["operation"] == "write":
              self.usb.expect_write(wire)
            else:
              self.assertEqual(operation["operation"], "read")
              self.usb.expect_read(wire)
        else:
          for chunk in encode_serial_write(request.encode()):
            self.usb.expect_write(chunk)
          raw = reply.encode()
          self.usb.expect_write(encode_serial_read(len(raw)))
          self.usb.expect_read(len(raw).to_bytes(2, "little") + raw)
        if request.command == 0x73:
          status = await self.driver.get_plate_status()
          self.assertEqual(status.raw_payload, reply.payload)
          self.assertIn(status.state, ("loaded", "moving", "unloaded"))
        else:
          self.assertEqual(request.command, 0x72)
          self.assertEqual(await self.driver._exchange(0x72, 0), b"")
        self.assertFalse(self.usb.script)

  async def test_release_and_repeated_calls_after_restart(self) -> None:
    """Loaded/moving/unloaded succeeds once; duplicate concurrent callers only observe."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    self.usb.release_poll(1)
    self.usb.release_poll(0)
    self.usb.state_checks()
    self.usb.release_preflight(0)
    self.usb.state_checks()
    first, second = await asyncio.gather(
      self.driver.release_plate(_RUN_ID), self.driver.release_plate(_RUN_ID)
    )
    self.assertEqual(first.state, "unloaded")
    self.assertEqual(second, first)
    self.assertEqual(self.usb.commands().count(0x72), 1)
    self.assertFalse(self.usb.script)
    self.assert_release_state("released")
    await self.restart()
    self.usb.release_preflight(0)
    self.usb.state_checks()
    self.assertEqual((await self.driver.release_plate(_RUN_ID)).state, "unloaded")
    self.assertNotIn(0x72, self.usb.commands())

  async def test_already_unloaded_needs_no_motion(self) -> None:
    """Desired state is confirmed using live evidence without dispatching release."""
    self.usb.release_preflight(0)
    self.usb.state_checks()
    await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())
    self.assert_release_state("released")

  async def test_status_without_journal(self) -> None:
    """Public observation requires no persistent run or mutation authorization."""
    observer = ABI7500FastDx()
    observer.io = cast(USB, self.usb)
    observer._connected = True
    self.usb.exchange(0x73, b"\x01\x00")
    self.assertEqual((await observer.get_plate_status()).state, "unknown")
    self.assertEqual(self.usb.commands(), [0x73])
    with self.assertRaisesRegex(ABI7500RunError, "run_journal"):
      await observer.release_plate(_RUN_ID)

  async def test_failed_intent_commit_prevents_dispatch(self) -> None:
    """A failed durable write must precede and prevent every release command."""
    self.usb.release_preflight()
    with patch.object(self.journal, "save", side_effect=sqlite3.OperationalError("disk full")):
      with self.assertRaises(sqlite3.OperationalError):
        await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())
    self.assert_release_state("not_requested")

  async def test_state_change_immediately_before_dispatch(self) -> None:
    """A plate read followed by a new thermal token cannot authorize release."""
    self.usb.identity()
    self.usb.state_checks()
    self.usb.exchange(0x73, b"\x00\x02")
    changed = bytearray(_IDLE)
    changed[32] ^= 1
    self.usb.exchange(0x29, bytes(changed))
    with self.assertRaisesRegex(ABI7500RunError, "same finished run"):
      await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())
    self.assert_release_state("not_requested")

  async def test_lost_ack_persists_intent_and_blocks_new_run(self) -> None:
    """A lost acknowledgement cannot be replayed across process replacement."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    self.usb.fail_command = 0x72
    with self.assertRaises(TimeoutError):
      await self.driver.release_plate(_RUN_ID)
    self.assert_release_state("uncertain")
    self.assertFalse(self.driver.is_connected)
    self.assertEqual(self.usb.commands().count(0x72), 1)
    await self.restart()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve plate release"):
      self.journal.reserve(_checkpoint("next-run"))
    with self.assertRaisesRegex(ABI7500RunError, "Resolve plate release"):
      self.journal.check_optical_control(_finished().camera_description, "G2.10")
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve plate release"):
      await self.driver.start_run("next-run", _finished().program)
    self.assertNotIn(0x1B, self.usb.commands())
    self.assertNotIn(0x13, self.usb.commands())
    self.usb.identity()
    self.usb.state_checks()
    self.usb.exchange(0x73, b"\x00\x02")
    self.usb.release_poll(1)
    self.usb.release_poll(0)
    self.usb.state_checks()
    await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())
    self.assert_release_state("released")
    self.journal.reserve(_checkpoint("next-run"))

  async def test_cancelled_await_keeps_intent(self) -> None:
    """Cancellation waits for the USB acknowledgement then preserves no-replay state."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    self.usb.block_command = 0x72
    task = asyncio.create_task(self.driver.release_plate(_RUN_ID))
    await self.usb.block_started.wait()
    self.assert_release_state("uncertain")
    task.cancel()
    self.usb.unblock.set()
    with self.assertRaises(asyncio.CancelledError):
      await task
    await self.restart()
    self.usb.release_preflight(0)
    self.usb.state_checks()
    await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())

  async def test_motion_timeout_never_resends(self) -> None:
    """Polling deadline expiry remains uncertain even with an acknowledged command."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    with self.assertRaises(TimeoutError):
      await self.driver.release_plate(_RUN_ID, timeout=1e-15)
    self.assert_release_state("uncertain")
    self.assertEqual(self.usb.commands().count(0x72), 1)
    self.usb.identity()
    self.usb.state_checks()
    self.usb.exchange(0x73, b"\x00\x02")
    with self.assertRaises(TimeoutError):
      await self.driver.release_plate(_RUN_ID, timeout=1e-15)
    self.assertEqual(self.usb.commands().count(0x72), 1)
    self.assertFalse(self.usb.script)

  async def test_slow_plate_reply_ends_polling_before_another_query(self) -> None:
    """A transfer crossing the motion deadline cannot start another full command budget."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    self.usb.exchange(0x73, b"\x00\x01")
    read = self.usb.read

    async def delayed_read(size: int, timeout: int | None = None) -> bytes:
      """Delay only the first plate observation after motion dispatch."""
      if self.usb.last_command == 0x73 and 0x72 in self.usb.commands():
        await _REAL_SLEEP(0.04)
      return await read(size, timeout)

    with patch.object(self.usb, "read", new=delayed_read):
      with self.assertRaises(TimeoutError):
        await self.driver.release_plate(_RUN_ID, timeout=0.02)
    self.assertFalse(self.usb.script)
    self.assertEqual(self.usb.commands()[-1], 0x73)
    self.assert_release_state("uncertain")

  async def test_unknown_and_changed_status_after_motion(self) -> None:
    """Unknown mechanism state or new thermal activity cannot certify release."""
    for plate, status in ((7, _IDLE), (0, _payload("acquire", "active_status"))):
      with self.subTest(plate=plate):
        self.journal.save(_finished())
        self.usb.release_preflight()
        self.usb.exchange(0x72)
        self.usb.release_poll(plate, status)
        with self.assertRaises(ABI7500RunError):
          await self.driver.release_plate(_RUN_ID)
        self.assert_release_state("uncertain")
        self.assertFalse(self.usb.script)

  async def test_final_history_change_does_not_confirm_release(self) -> None:
    """Unloaded readback alone cannot override changed run identity at the final check."""
    self.usb.release_preflight()
    self.usb.exchange(0x72)
    self.usb.release_poll(0)
    self.usb.exchange(0x29, _IDLE)
    self.usb.exchange(0x19, _history("acquire", "other-run"))
    with self.assertRaisesRegex(ABI7500RunError, "history"):
      await self.driver.release_plate(_RUN_ID)
    self.assert_release_state("uncertain")

  async def test_input_and_checkpoint_guards(self) -> None:
    """Bad arguments, unknown IDs and unfinished runs are refused before USB I/O."""
    for timeout in (0, -1, float("nan"), float("inf")):
      with self.assertRaises(ValueError):
        await self.driver.release_plate(_RUN_ID, timeout=timeout)
    with self.assertRaises(ABI7500RunError):
      await self.driver.release_plate("unknown")
    for run in (
      _running(),
      replace(_finished(), start_record=None),
      replace(_finished(), state="not_started"),
    ):
      self.journal.save(run)
      with self.assertRaisesRegex(ABI7500RunError, "reconciled finished"):
        await self.driver.release_plate(_RUN_ID)
    self.assertFalse(self.usb.writes)

  async def test_preflight_rejections(self) -> None:
    """Reject active/stale history, lit/moving optics and changed status before motion."""
    cases: list[_StateChecks] = [
      dict(status=_payload("acquire", "active_status")),
      dict(history=_history("acquire", "other-run")),
      dict(history=_history("cancel", _RUN_ID)),
      dict(position=5),
      dict(motor=1),
      dict(lamp=b"\x00\x01\x00\x01"),
      dict(after=_payload("acquire", "active_status")),
    ]
    for options in cases:
      with self.subTest(options=options):
        await self.restart()
        self.usb.identity()
        self.usb.state_checks(**options)
        with self.assertRaises(ABI7500RunError):
          await self.driver.release_plate(_RUN_ID)
        self.assertNotIn(0x72, self.usb.commands())
        self.assert_release_state("not_requested")

  async def test_moving_unknown_and_reloaded_plate_rejected(self) -> None:
    """No first command is issued to an unknown/moving plate or for an already-used release."""
    for code in (1, 9):
      self.usb.release_preflight(code)
      with self.assertRaises(ABI7500RunError):
        await self.driver.release_plate(_RUN_ID)
      self.assertNotIn(0x72, self.usb.commands())
      self.assertFalse(self.usb.script)
    self.journal.save(replace(_finished(), plate_release_state="released"))
    self.usb.identity()
    self.usb.state_checks()
    self.usb.exchange(0x73, b"\x00\x02")
    with self.assertRaisesRegex(ABI7500RunError, "cannot release again"):
      await self.driver.release_plate(_RUN_ID)
    self.assertNotIn(0x72, self.usb.commands())

  async def test_wrong_instrument_and_other_unresolved_run(self) -> None:
    """The stored completed outcome cannot authorize another device or override a pending run."""
    self.journal.save(replace(_finished(), camera_description="different camera"))
    self.usb.identity()
    with self.assertRaisesRegex(ABI7500RunError, "does not match"):
      await self.driver.release_plate(_RUN_ID)
    self.journal.save(_finished())
    self.journal.reserve(_checkpoint("another-run"))
    before = list(self.usb.writes)
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      await self.driver.release_plate(_RUN_ID)
    self.assertEqual(self.usb.writes, before)

  async def test_firmware_error_never_retried(self) -> None:
    """A nonzero native error is retained and does not erase the release intent."""
    self.usb.release_preflight()
    self.usb.sequence += 1
    self.usb.query(0x72, self.usb.sequence, b"", error=7)
    with self.assertRaises(ABI7500DeviceError):
      await self.driver.release_plate(_RUN_ID)
    self.assert_release_state("uncertain")
    self.assertEqual(self.usb.commands().count(0x72), 1)

  async def test_aborted_and_cancelled_runs_can_release(self) -> None:
    """Both terminal abort states require matching aborted history, never implicit cancellation."""
    status = _payload("cancel", "inactive_status")
    history = _history("cancel", _RUN_ID)
    for outcome in ("aborted", "cancelled"):
      self.journal.save(replace(_running(), state=outcome))  # type: ignore[arg-type]
      self.usb.identity()
      self.usb.state_checks(status=status, history=history, after=status)
      self.usb.exchange(0x73, b"\x00\x00")
      self.usb.state_checks(status=status, history=history, after=status)
      await self.driver.release_plate(_RUN_ID)
      self.assert_release_state("released")
    self.assertNotIn(0x07, self.usb.commands())
    self.assertNotIn(0x72, self.usb.commands())
