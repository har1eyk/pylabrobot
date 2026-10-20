"""Exercise run ownership and recovery using captured bytes and explicitly synthetic run tags."""

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Optional, cast
from unittest.mock import patch

from pylabrobot.io.usb import USB

from ._run_journal import RunJournal
from .driver import ABI7500FastDx
from .driver_tests import _CAMERA, _VERSION, _ScriptedUSB
from .protocol import ABI7500ProtocolError, ThermalPacket, encode_serial_read, encode_serial_write
from .run import ABI7500History, ABI7500RunError, ThermalRun
from .status import ABI7500Status
from .thermal_program import ThermalProgram

_FIXTURE = json.loads((Path(__file__).parent / "test_data/run_control_vectors.json").read_text())
_RUN_ID = "plr-test-001"


def _payload(session: str, name: str, direction: str = "reply") -> bytes:
  """Extract an unmodified payload from a captured request/reply pair."""
  return bytes.fromhex(_FIXTURE["sessions"][session]["exchanges"][name][direction]["payload_hex"])


_PROGRAM = ThermalProgram.decode(_payload("acquire", "upload", "request"))
_BASELINE = ABI7500Status(_payload("acquire", "baseline_status"))


def _history(session: str, run_id: Optional[str] = None, *, baseline: bool = False) -> bytes:
  """Use captured history, optionally substituting a synthetic PLR run tag or prior start date."""
  data = bytearray(_payload(session, "history"))
  if run_id is not None:
    data[46:63] = run_id.encode().ljust(17, b"\x00")
    data[63:70] = b"PLR\x00\x00\x00\x00"
  if baseline:
    data[10:19] = _BASELINE.run_start_record
  return bytes(data)


def _checkpoint(run_id: str = _RUN_ID) -> ThermalRun:
  """Create a valid pre-start intent pinned to the captured reference instrument."""
  return ThermalRun(
    run_id, _PROGRAM, _CAMERA.split(b"\x00")[0].decode(), "G2.10", _BASELINE.run_start_record
  )


def _running(session: str = "cancel") -> ThermalRun:
  """Create a checkpoint representing an acknowledged start with a fresh observed date record."""
  status = ABI7500Status(_payload(session, "active_status"))
  return replace(
    _checkpoint(),
    state="running",
    start_acknowledged=True,
    start_record=status.run_start_record,
    last_status=status,
  )


class _RunUSB(_ScriptedUSB):
  """Ordered transport fake supporting payload writes and failures at a selected command."""

  def __init__(self) -> None:
    """Create a disconnected script; no real USB object is opened."""
    super().__init__()
    self.sequence = 0
    self.last_command: Optional[int] = None
    self.fail_command: Optional[int] = None
    self.block_command: Optional[int] = None
    self.block_started = asyncio.Event()
    self.unblock = asyncio.Event()

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Track the thermal command without losing multi-transfer write boundaries."""
    if data.startswith(b"RS"):
      self.last_command = int.from_bytes(data[18:20], "big")
    await super().write(data, timeout)

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Inject an ambiguous read failure or a cancellable in-flight start acknowledgement."""
    if self.last_command is not None and self.last_command == self.fail_command:
      raise TimeoutError("Lost command acknowledgement")
    if self.last_command is not None and self.last_command == self.block_command:
      self.block_started.set()
      await self.unblock.wait()
    return await super().read(size, timeout)

  def exchange(self, command: int, reply: bytes = b"", request: bytes = b"") -> None:
    """Append a complete successful exchange with exact framing and incrementing sequence."""
    self.sequence += 1
    for chunk in encode_serial_write(ThermalPacket(command, self.sequence, request).encode()):
      self.expect_write(chunk)
    wire = ThermalPacket(command, self.sequence, reply).encode()
    self.expect_write(encode_serial_read(len(wire)))
    self.expect_read(len(wire).to_bytes(2, "little") + wire)

  def identity(self) -> None:
    """Script the public identity query."""
    self.last_command = None
    self.expect_write(b"CF  64")
    self.expect_read(_CAMERA)
    self.exchange(0x0A, _VERSION)

  def observation(self, status: bytes, history: bytes, after: Optional[bytes] = None) -> None:
    """Script status/history/status bracketing, optionally with a transition during the read."""
    self.exchange(0x29, status)
    self.exchange(0x19, history)
    self.exchange(0x29, status if after is None else after)

  def preflight(self) -> None:
    """Script reference firmware and a coherent inactive baseline."""
    self.identity()
    self.observation(_BASELINE.raw_payload, _history("acquire", baseline=True))

  def start(self, *, load: bool = False) -> None:
    """Script the restricted thermal-only sequence, omitting all optical commands."""
    self.preflight()
    self.exchange(0x1B)
    self.exchange(0x12, request=_checkpoint().wire_program.encode())
    self.exchange(0x73, b"\x00\x00" if load else b"\x00\x02")
    if load:
      self.exchange(0x71)
      self.exchange(0x73, b"\x00\x01")
      self.exchange(0x73, b"\x00\x02")
    self.exchange(0x29, _BASELINE.raw_payload)
    self.exchange(0x13)

  def commands(self) -> list[int]:
    """Return thermal command IDs actually submitted, including partial upload packets."""
    return [int.from_bytes(w[18:20], "big") for w in self.writes if w.startswith(b"RS")]


class RunEvidenceTests(unittest.TestCase):
  """Validate captured history interpretation and checkpoint representation."""

  def test_completed_and_aborted_history(self) -> None:
    """Native outcomes and start dates agree across independent status/history replies."""
    for session, expected, duration in (("acquire", "completed", 290), ("cancel", "aborted", 42)):
      history = ABI7500History(_payload(session, "history"))
      status = ABI7500Status(_payload(session, "active_status"))
      self.assertEqual(history.outcome, expected)
      self.assertEqual(history.duration, duration)
      self.assertEqual(history.reaction_volume, 20)
      self.assertEqual(history.method_name, "Msmall")
      self.assertEqual(history.user_name, "Maya")
      self.assertEqual(history.run_start_record, status.run_start_record)
    for code, outcome in ((253, "no_result"), (9, "unknown")):
      raw = bytearray(_payload("acquire", "history"))
      raw[6:8] = code.to_bytes(2, "big")
      self.assertEqual(ABI7500History(bytes(raw)).outcome, outcome)
    for size in (0, 93, 95):
      with self.assertRaises(ABI7500ProtocolError):
        ABI7500History(bytes(size))

  def test_run_tag_and_checkpoint_roundtrip(self) -> None:
    """Only bounded name/user fields change; thermal records and opaque dates are retained."""
    run = _running()
    self.assertEqual(run.wire_program.encode()[25:], run.program.encode()[25:])
    self.assertEqual(run.wire_program.metadata[:17], _RUN_ID.encode().ljust(17, b"\x00"))
    self.assertEqual(run.wire_program.metadata[17:24], b"PLR\x00\x00\x00\x00")
    self.assertEqual(ThermalRun.from_dict(json.loads(json.dumps(run.to_dict()))), run)
    for run_id in ("", "too-long-run-name-001", "bad\x00id", "é"):
      with self.assertRaises(ValueError):
        _checkpoint(run_id)

  def test_journal_exclusivity_durability_and_pending_guard(self) -> None:
    """Another controller cannot inspect/mutate the journal until the first releases ownership."""
    with tempfile.TemporaryDirectory() as root:
      path = Path(root) / "run.sqlite3"
      first, second = RunJournal(path), RunJournal(path)
      try:
        first.reserve(_checkpoint())
        with self.assertRaises(sqlite3.OperationalError):
          second.get(_RUN_ID)
        with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
          first.reserve(_checkpoint("different"))
        first.close()
        self.assertEqual(second.get(_RUN_ID), _checkpoint())
        second.save(replace(_checkpoint(), state="not_started"))
        with self.assertRaisesRegex(ABI7500RunError, "different instrument"):
          second.reserve(replace(_checkpoint("different"), camera_description="other camera"))
      finally:
        first.close()
        second.close()

  def test_checkpoint_waits_for_brief_read_only_inspection(self) -> None:
    """A concurrent read snapshot must not discard a checkpoint when its commit needs the lock."""
    with tempfile.TemporaryDirectory() as root:
      path = Path(root) / "run.sqlite3"
      journal = RunJournal(path)
      reader_ready, committing = Event(), Event()

      def read_snapshot() -> None:
        """Hold a read transaction until the writer attempts its checkpoint commit."""
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
          reader.execute("BEGIN")
          reader.execute("SELECT record FROM runs").fetchall()
          reader_ready.set()
          if not committing.wait(5):
            raise AssertionError("Writer did not attempt its commit")
          time.sleep(0.05)
        finally:
          reader.close()

      def observe_commit(statement: str) -> None:
        """Coordinate the reader with SQLite's actual transaction boundary."""
        if statement == "COMMIT":
          committing.set()

      try:
        journal.reserve(_checkpoint())
        journal._open().set_trace_callback(observe_commit)
        with ThreadPoolExecutor(max_workers=1) as executor:
          reader = executor.submit(read_snapshot)
          self.assertTrue(reader_ready.wait(5))
          updated = replace(_checkpoint(), state="not_started")
          journal.save(updated)
          reader.result(timeout=5)
        journal.close()
        self.assertEqual(journal.get(_RUN_ID), updated)
      finally:
        committing.set()
        journal.close()


class RunDriverTests(unittest.IsolatedAsyncioTestCase):
  """Run retries and cancellation must never act on unproven instrument state."""

  async def asyncSetUp(self) -> None:
    """Use an isolated local journal and an already-connected transport fake."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "run.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _RunUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True

  async def asyncTearDown(self) -> None:
    """Release controller ownership and all fake transport tasks."""
    self.usb.unblock.set()
    await self.driver.stop()
    self.temp.cleanup()

  async def restart(self) -> None:
    """Simulate process replacement without discarding the durable run journal."""
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _RunUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True

  async def test_start_is_once_and_reconciles_delayed_status(self) -> None:
    """A successful Start can initially return old status, without permitting another Start."""
    self.usb.start(load=True)
    run, duplicate = await asyncio.gather(
      self.driver.start_run(_RUN_ID, _PROGRAM), self.driver.start_run(_RUN_ID, _PROGRAM)
    )
    self.assertEqual(run, duplicate)
    self.assertEqual(run.state, "starting")
    self.assertTrue(run.start_acknowledged)
    writes = list(self.usb.writes)
    self.assertEqual(await self.driver.start_run(_RUN_ID, _PROGRAM), run)
    self.assertEqual(self.usb.writes, writes)
    self.usb.identity()
    self.usb.observation(_payload("acquire", "delayed_status"), _history("acquire", baseline=True))
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "starting")
    self.usb.identity()
    self.usb.observation(_payload("acquire", "active_status"), _history("acquire", baseline=True))
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "running")
    self.usb.identity()
    self.usb.observation(_payload("acquire", "inactive_status"), _history("acquire", _RUN_ID))
    finished = await self.driver.get_run(_RUN_ID)
    self.assertEqual(finished.state, "completed")
    self.assertEqual(self.usb.commands().count(0x13), 1)
    self.assertEqual(self.usb.commands().count(0x71), 1)
    self.assertFalse(self.usb.script)
    await self.restart()
    self.assertEqual(await self.driver.start_run(_RUN_ID, _PROGRAM), finished)
    self.assertFalse(self.usb.writes)

  async def test_lost_start_ack_never_resends_and_requires_tagged_evidence(self) -> None:
    """After reconnect, new temperatures or elapsed time alone cannot establish run ownership."""
    self.usb.start()
    self.usb.fail_command = 0x13
    with self.assertRaises(TimeoutError):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertEqual(self.usb.commands().count(0x13), 1)
    await self.restart()
    run = await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertEqual(run.state, "uncertain")
    self.assertFalse(self.usb.writes)
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("acquire", baseline=True))
    run = await self.driver.get_run(_RUN_ID)
    self.assertEqual(run.state, "uncertain")
    self.assertIsNone(run.start_record)
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("acquire", baseline=True))
    with self.assertRaisesRegex(ABI7500RunError, "ownership is uncertain"):
      await self.driver.cancel_run(_RUN_ID)
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("cancel", _RUN_ID))
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "running")
    self.assertNotIn(0x13, self.usb.commands())
    self.assertNotIn(0x07, self.usb.commands())
    self.assertFalse(self.usb.script)

  async def test_pending_run_and_changed_program_cannot_bypass_identity(self) -> None:
    """Different arguments or IDs cannot turn an ambiguous checkpoint into another execution."""
    self.driver._require_journal().reserve(replace(_checkpoint(), state="uncertain"))
    changed = ThermalProgram(_PROGRAM.metadata, _PROGRAM.cycles[:1])
    with self.assertRaisesRegex(ABI7500RunError, "different program"):
      await self.driver.start_run(_RUN_ID, changed)
    self.assertFalse(self.usb.writes)
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      await self.driver.start_run("different", _PROGRAM)
    self.assertNotIn(0x1B, self.usb.commands())

  async def test_cancel_confirms_inactive_before_idle_and_is_idempotent(self) -> None:
    """Stop precedes status confirmation, and cooling is never inferred from its acknowledgement."""
    self.driver._require_journal().reserve(_running())
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("acquire", baseline=True))
    self.usb.exchange(0x07)
    self.usb.exchange(0x29, _payload("cancel", "active_status"))
    self.usb.exchange(0x29, _payload("cancel", "inactive_status"))
    self.usb.exchange(0x22, request=b"\x00\xfa")
    self.usb.observation(_payload("cancel", "inactive_status"), _history("cancel", _RUN_ID))
    run = await self.driver.cancel_run(_RUN_ID)
    self.assertEqual(run.state, "cancelled")
    self.assertTrue(run.idle_temperature_set)
    self.assertIsNotNone(run.last_status)
    assert run.last_status is not None
    self.assertGreater(run.last_status.sample_temperature, 25)
    writes = list(self.usb.writes)
    self.assertEqual(await self.driver.cancel_run(_RUN_ID), run)
    self.assertEqual(self.usb.writes, writes)
    self.assertEqual(self.usb.commands().count(0x07), 1)
    self.assertFalse(self.usb.script)

  async def test_lost_stop_ack_reconciles_without_stopping_again(self) -> None:
    """A stopped run after reconnect needs its idle target, not a second Stop command."""
    self.driver._require_journal().reserve(_running())
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("acquire", baseline=True))
    self.usb.exchange(0x07)
    self.usb.fail_command = 0x07
    with self.assertRaises(TimeoutError):
      await self.driver.cancel_run(_RUN_ID)
    await self.restart()
    self.usb.identity()
    self.usb.observation(_payload("cancel", "inactive_status"), _history("cancel", _RUN_ID))
    self.usb.exchange(0x29, _payload("cancel", "inactive_status"))
    self.usb.exchange(0x22, request=b"\x00\xfa")
    self.usb.observation(_payload("cancel", "inactive_status"), _history("cancel", _RUN_ID))
    self.assertEqual((await self.driver.cancel_run(_RUN_ID)).state, "cancelled")
    self.assertNotIn(0x07, self.usb.commands())
    self.assertFalse(self.usb.script)

  async def test_foreign_run_or_transition_cannot_be_cancelled(self) -> None:
    """A different start record, even with a familiar completed history label, is not owned."""
    self.driver._require_journal().reserve(_running())
    self.usb.identity()
    self.usb.observation(_payload("acquire", "active_status"), _history("cancel", _RUN_ID))
    with self.assertRaisesRegex(ABI7500RunError, "ownership is uncertain"):
      await self.driver.cancel_run(_RUN_ID)
    self.usb.identity()
    self.usb.observation(
      _payload("cancel", "active_status"),
      _history("cancel", _RUN_ID),
      after=_payload("cancel", "inactive_status"),
    )
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "uncertain")
    self.assertNotIn(0x07, self.usb.commands())
    self.assertNotIn(0x22, self.usb.commands())

  async def test_task_cancellation_preserves_durable_start_intent(self) -> None:
    """A cancelled host task cannot erase the possibility that Start reached the instrument."""
    self.usb.start()
    self.usb.block_command = 0x13
    task = asyncio.create_task(self.driver.start_run(_RUN_ID, _PROGRAM))
    await self.usb.block_started.wait()
    with sqlite3.connect(self.path) as db:
      saved = json.loads(db.execute("SELECT record FROM runs").fetchone()[0])
    self.assertEqual(saved["state"], "starting")
    self.assertFalse(saved["start_acknowledged"])
    task.cancel()
    await asyncio.sleep(0)
    self.assertFalse(task.done())
    self.usb.unblock.set()
    with self.assertRaises(asyncio.CancelledError):
      await task
    await self.restart()
    self.assertEqual((await self.driver.start_run(_RUN_ID, _PROGRAM)).state, "uncertain")
    self.assertFalse(self.usb.writes)

  async def test_failure_before_start_does_not_claim_execution(self) -> None:
    """A failed upload records that Start was never attempted, and is not automatically retried."""
    self.usb.start()
    self.usb.fail_command = 0x12
    with self.assertRaises(TimeoutError):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertNotIn(0x13, self.usb.commands())
    await self.restart()
    self.assertEqual((await self.driver.start_run(_RUN_ID, _PROGRAM)).state, "not_started")
    self.assertFalse(self.usb.writes)

  async def test_preflight_rejects_active_and_unsupported_configuration(self) -> None:
    """Invalid configuration fails before USB; active sequences prohibit upload/load/start."""
    invalid = ThermalProgram(_PROGRAM.metadata[:-5] + bytes.fromhex("3200020000"), _PROGRAM.cycles)
    with self.assertRaises(ValueError):
      await self.driver.start_run(_RUN_ID, invalid)
    self.assertFalse(self.usb.writes)
    self.usb.identity()
    self.usb.observation(_payload("acquire", "active_status"), _history("acquire", baseline=True))
    with self.assertRaisesRegex(ABI7500RunError, "active, transitioning"):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertNotIn(0x12, self.usb.commands())
    self.assertNotIn(0x71, self.usb.commands())

  async def test_captured_upload_uses_exact_two_out_transfers(self) -> None:
    """Replay the unmodified vendor upload to check request payload framing independently."""
    vector = _FIXTURE["sessions"]["acquire"]["exchanges"]["upload"]
    request = ThermalPacket.decode(bytes.fromhex(vector["request"]["packet_hex"]))
    self.driver._sequence = request.sequence - 1
    self.usb.sequence = request.sequence - 1
    self.usb.exchange(0x12, request=request.payload)
    await self.driver._exchange(0x12, 0, request.payload)
    captured = json.loads((Path(__file__).parent / "test_data/usb_run_vectors.json").read_text())[
      "vectors"
    ][0]
    self.assertEqual(self.usb.writes[:2], [bytes.fromhex(h) for h in captured["usb_write_hex"]])
    self.assertFalse(self.usb.script)

  async def test_history_without_matching_label_cannot_prove_completion(self) -> None:
    """A completed history belonging to a different method cannot resolve the PLR intent."""
    self.driver._require_journal().reserve(_running("acquire"))
    self.usb.identity()
    self.usb.observation(_payload("acquire", "inactive_status"), _history("acquire"))
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "uncertain")
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      await self.driver.start_run("different", _PROGRAM)
    self.assertNotIn(0x13, self.usb.commands())

  async def test_cancel_timeout_does_not_imply_stopped_or_request_cooling(self) -> None:
    """A still-active sequence must not receive the idle target or a cancelled result."""
    self.driver._require_journal().reserve(_running())
    self.usb.identity()
    self.usb.observation(_payload("cancel", "active_status"), _history("acquire", baseline=True))
    self.usb.exchange(0x07)
    self.usb.exchange(0x29, _payload("cancel", "active_status"))
    with self.assertRaises(TimeoutError):
      await self.driver.cancel_run(_RUN_ID, timeout=1e-12)
    run = self.driver._require_journal().get(_RUN_ID)
    assert run is not None
    self.assertEqual(run.state, "uncertain")
    self.assertNotIn(0x22, self.usb.commands())

  async def test_history_id_prevents_restart_with_missing_local_checkpoint(self) -> None:
    """The last hardware history provides an additional guard against a missing local record."""
    self.usb.identity()
    self.usb.observation(_BASELINE.raw_payload, _history("acquire", _RUN_ID))
    with self.assertRaisesRegex(ABI7500RunError, "already contains this run ID"):
      await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertNotIn(0x1B, self.usb.commands())

  async def test_different_instrument_cannot_reconcile_or_cancel(self) -> None:
    """Matching temperatures and run times do not authorize acting on another physical camera."""
    self.driver._require_journal().reserve(replace(_running(), camera_description="other"))
    self.usb.identity()
    with self.assertRaisesRegex(ABI7500RunError, "does not match"):
      await self.driver.cancel_run(_RUN_ID)
    self.assertEqual(self.usb.commands(), [0x0A])

  async def test_cancelled_preparation_does_not_mark_start_as_sent(self) -> None:
    """A durable preparing record after process loss is resolved without starting the program."""
    self.driver._require_journal().reserve(_checkpoint())
    await self.restart()
    self.usb.preflight()
    self.assertEqual((await self.driver.get_run(_RUN_ID)).state, "not_started")
    self.assertNotIn(0x13, self.usb.commands())

  async def test_journal_commit_failure_prevents_start_write(self) -> None:
    """A failed durable starting checkpoint cannot be followed by an unsafe wire submission."""
    journal = self.driver._require_journal()
    original_save = journal.save

    def fail_start_checkpoint(run: ThermalRun) -> ThermalRun:
      """Simulate a disk failure only at the pre-Start transaction boundary."""
      if run.state == "starting":
        raise OSError("journal commit failed")
      return original_save(run)

    self.usb.start()
    with patch.object(journal, "save", side_effect=fail_start_checkpoint):
      with self.assertRaisesRegex(OSError, "journal commit failed"):
        await self.driver.start_run(_RUN_ID, _PROGRAM)
    self.assertNotIn(0x13, self.usb.commands())
    run = journal.get(_RUN_ID)
    assert run is not None
    self.assertEqual(run.state, "not_started")
