"""Replay original Ubuntu thermal-run evidence without opening hardware."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from pylabrobot.io.usb import USB

from .driver import ABI7500FastDx
from .driver_tests import _ScriptedUSB
from .protocol import ThermalPacket
from .run import ThermalRun
from .run_tests import _RunUSB

_FIXTURE = json.loads((Path(__file__).parent / "test_data/ubuntu_run_vectors.json").read_text())
_HOLD_FIXTURE = json.loads(
  (Path(__file__).parent / "test_data/ubuntu_hold_vectors.json").read_text()
)


def _captured_run(name: str) -> ThermalRun:
  """Restore an unmodified checkpoint from the approved Ubuntu session."""
  return ThermalRun.from_dict(_FIXTURE["checkpoints"][name])


class UbuntuRunTests(unittest.IsolatedAsyncioTestCase):
  """Validate live-observed framing, delayed startup, ownership and cancellation offline."""

  def test_active_history_has_no_final_result(self) -> None:
    """Code 253 occurs during a positively identified active run, not only before any run."""
    active = _captured_run("running")
    assert active.history is not None and active.last_status is not None
    self.assertEqual(active.last_status.phase, "ramping")
    self.assertEqual(active.history.method_name, active.run_id)
    self.assertEqual(active.history.run_start_record, active.last_status.run_start_record)
    self.assertEqual(active.history.status_code, 253)
    self.assertEqual(active.history.outcome, "no_result")

  async def test_exact_captured_thermal_exchanges(self) -> None:
    """Mutations and plate queries retain actual USB boundaries and counted reply fragments."""
    driver = ABI7500FastDx()
    usb = _ScriptedUSB()
    driver.io = cast(USB, usb)
    driver._connected = True
    try:
      for vector in (*_FIXTURE["exchanges"].values(), *_HOLD_FIXTURE["exchanges"].values()):
        request = ThermalPacket.decode(bytes.fromhex(vector["request_hex"]))
        reply = ThermalPacket.decode(bytes.fromhex(vector["reply_hex"]))
        driver._sequence = request.sequence - 1
        for operation in vector["usb_operations"]:
          data = bytes.fromhex(operation["data_hex"])
          if operation["operation"] == "write":
            usb.expect_write(data)
          else:
            usb.expect_read(data)
        self.assertEqual(
          await driver._exchange(request.command, len(reply.payload), request.payload),
          reply.payload,
        )
        self.assertFalse(usb.script)
    finally:
      await driver.stop()

  async def test_captured_hold_completes_only_after_final_history_and_idle(self) -> None:
    """Reconcile captured boundary transitions before retaining normal completion."""
    initial = ThermalRun.from_dict(_HOLD_FIXTURE["checkpoints"]["start_acknowledged"])
    with tempfile.TemporaryDirectory() as root:
      path = Path(root) / "runs.sqlite3"
      driver = ABI7500FastDx(run_journal=path)
      usb = _RunUSB()
      driver.io = cast(USB, usb)
      driver._connected = True
      driver._require_journal().reserve(initial)
      try:
        for name, observation in _HOLD_FIXTURE["observations"].items():
          usb.identity()
          usb.observation(
            bytes.fromhex(observation["before_status_hex"]),
            bytes.fromhex(observation["history_hex"]),
            after=bytes.fromhex(observation["after_status_hex"]),
          )
          expected = ThermalRun.from_dict(_HOLD_FIXTURE["checkpoints"][name])
          self.assertEqual(await driver.get_run(initial.run_id), expected)
        finished = ThermalRun.from_dict(_HOLD_FIXTURE["checkpoints"]["completed"])
        assert finished.history is not None and finished.last_status is not None
        self.assertEqual(finished.state, "completed")
        self.assertEqual(finished.history.duration, 73)
        self.assertEqual(finished.last_status.sample_temperature, 35)
        self.assertFalse(finished.cancel_requested)
        self.assertFalse(finished.idle_temperature_set)
        self.assertFalse(usb.script)
        self.assertTrue(set(usb.commands()) <= {0x0A, 0x19, 0x29})
        await driver.stop()
        driver = ABI7500FastDx(run_journal=path)
        driver.io = cast(USB, usb)
        writes = list(usb.writes)
        self.assertEqual(await driver.get_run(initial.run_id), finished)
        self.assertEqual(await driver.start_run(initial.run_id, initial.program), finished)
        self.assertEqual(usb.writes, writes)
      finally:
        await driver.stop()

  async def test_startup_reconnect_and_cancellation_with_original_labels(self) -> None:
    """Stale aborted history during cover warmup cannot resolve or restart the new intent."""
    initial = _captured_run("start_acknowledged")
    with tempfile.TemporaryDirectory() as root:
      path = Path(root) / "runs.sqlite3"
      driver = ABI7500FastDx(run_journal=path)
      usb = _RunUSB()
      driver.io = cast(USB, usb)
      driver._connected = True
      driver._require_journal().reserve(initial)
      try:
        for name in ("early_startup", "late_startup", "running"):
          captured = _captured_run(name)
          assert captured.last_status is not None and captured.history is not None
          usb.identity()
          usb.observation(captured.last_status.raw_payload, captured.history.raw_payload)
          observed = await driver.get_run(initial.run_id)
          self.assertEqual(observed.state, captured.state)
          self.assertEqual(observed.start_record, captured.start_record)
          if name == "early_startup":
            self.assertEqual(captured.history.outcome, "aborted")
            self.assertNotEqual(captured.history.method_name, initial.run_id)
            await driver.stop()
            driver = ABI7500FastDx(run_journal=path)
            usb = _RunUSB()
            driver.io = cast(USB, usb)
            driver._connected = True
            self.assertEqual(await driver.start_run(initial.run_id, initial.program), observed)
            self.assertFalse(usb.writes)
        active, finished = _captured_run("running"), _captured_run("cancelled")
        assert active.last_status is not None and active.history is not None
        assert finished.last_status is not None and finished.history is not None
        usb.identity()
        usb.observation(active.last_status.raw_payload, active.history.raw_payload)
        usb.exchange(0x07)
        usb.exchange(0x29, finished.last_status.raw_payload)
        usb.exchange(0x22, request=b"\x00\xfa")
        usb.observation(finished.last_status.raw_payload, finished.history.raw_payload)
        cancelled = await driver.cancel_run(initial.run_id)
        self.assertEqual(cancelled, finished)
        self.assertEqual(finished.history.method_name, initial.run_id)
        self.assertEqual(finished.history.user_name, "PLR")
        self.assertEqual(finished.history.outcome, "aborted")
        self.assertNotIn(0x13, usb.commands())
        self.assertEqual(usb.commands().count(0x07), 1)
        self.assertFalse(usb.script)
        writes = list(usb.writes)
        self.assertEqual(await driver.cancel_run(initial.run_id), finished)
        self.assertEqual(usb.writes, writes)
      finally:
        await driver.stop()
