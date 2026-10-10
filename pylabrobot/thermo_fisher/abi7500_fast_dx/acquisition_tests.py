"""Replay camera traffic and exercise snapshot failures without opening hardware."""

import asyncio
import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .acquisition import CameraAcquisition, CameraExposure
from .driver import ABI7500FastDx
from .driver_tests import _CAMERA, _VERSION, _ScriptedUSB
from .protocol import ABI7500ProtocolError
from .run import ABI7500RunError, ThermalRun

_DATA = Path(__file__).parent / "test_data"
_COMMANDS = json.loads((_DATA / "camera_commands.json").read_text())
_CAPTURE = json.loads((_DATA / "camera_capture_vector.json").read_text())
_IMAGE = zlib.decompress(base64.b64decode(_CAPTURE["payload"]))
_HOLD = json.loads((_DATA / "ubuntu_hold_vectors.json").read_text())
_SNAPSHOT = json.loads((_DATA / "ubuntu_snapshot_vector.json").read_text())
_IDLE = bytes.fromhex(_HOLD["observations"]["completed"]["after_status_hex"])
_ACTIVE = bytes.fromhex(_HOLD["observations"]["running"]["after_status_hex"])
_SLEEP = asyncio.sleep


class _CameraUSB(_ScriptedUSB):
  """Script snapshot preflight and retain exact camera command/payload boundaries."""

  def __init__(self) -> None:
    """Start an isolated scripted connection with independent thermal sequence tracking."""
    super().__init__()
    self.sequence = 0

  def thermal(self, command: int, payload: bytes) -> None:
    """Append a thermal query with a new synthetic sequence and captured reply payload."""
    self.sequence += 1
    self.query(command, self.sequence, payload)

  def optics(self, motor: bytes = b"\x00\x00", position: bytes = b"\x00\x01") -> None:
    """Append the native optical status observations without actuating the optics."""
    self.thermal(0x6C, motor)
    self.thermal(0x6B, position)
    self.thermal(0x66, b"\x00\x01\x00\x00")

  def preflight(self, status: bytes = _IDLE, motor: bytes = b"\x00\x00") -> None:
    """Require fresh identity, dimensions, bracketed status and camera readiness."""
    self.expect_write(b"CF  64")
    self.expect_read(_CAMERA)
    self.thermal(0x0A, _VERSION)
    self.expect_write(b"CI   4")
    self.expect_read(bytes.fromhex(_CAPTURE["ci_reply_hex"]))
    self.thermal(0x29, status)
    self.optics(motor)
    self.thermal(0x29, status)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")

  def capture(self) -> None:
    """Replay every CJ/CC/CH/CD transfer for the existing captured image fixture."""
    for transfer in _COMMANDS["frame_sequence"]:
      data = bytes.fromhex(transfer["hex"])
      if transfer["action"] == "write":
        self.expect_write(data)
      else:
        self.expect_read(data)

  def image(self, *, odd_fragment: bool = False) -> None:
    """Queue the original frame, optionally splitting across a pixel boundary."""
    offset = 0
    if odd_fragment:
      self.expect_read(_IMAGE[:7], 4096)
      offset = 7
    while offset < len(_IMAGE):
      size = min(4096, len(_IMAGE) - offset)
      self.expect_read(_IMAGE[offset : offset + size], size)
      offset += size

  def final(self, status: bytes = _IDLE) -> None:
    """Append observations after the complete image transfer."""
    self.optics()
    self.thermal(0x29, status)


class ExposureTests(unittest.TestCase):
  """Validate encoding against vendor and Ubuntu captures and native conversion boundaries."""

  def test_captured_exposure_echoes(self) -> None:
    """Captured little-endian replies agree with every CJ millisecond field."""
    for vector in _COMMANDS["exposures"]:
      command = bytes.fromhex(vector["command_hex"])
      exposure = CameraExposure(int(command.split(b",")[1]) / 1000)
      self.assertEqual(exposure.encode(), command)
      exposure.validate_reply(bytes.fromhex(vector["reply_hex"]))

  def test_ubuntu_short_exposure_echoes(self) -> None:
    """Replay the six settings acknowledged during the 30-frame short-exposure session."""
    vectors = json.loads((_DATA / "ubuntu_short_exposure_vectors.json").read_text())["exposures"]
    self.assertEqual(sum(v["count"] for v in vectors), 30)
    self.assertEqual({v["milliseconds"] for v in vectors}, {5, 10, 30, 60, 180, 540})
    for vector in vectors:
      with self.subTest(milliseconds=vector["milliseconds"]):
        exposure = CameraExposure(vector["milliseconds"] / 1000)
        self.assertEqual(exposure.encode(), bytes.fromhex(vector["command_hex"]))
        exposure.validate_reply(bytes.fromhex(vector["reply_hex"]))

  def test_rounding_and_limits(self) -> None:
    """Respect the native 0.49 ms bias instead of rounding ties to even."""
    for duration, milliseconds in ((0.005, 5), (0.2565, 256), (0.25652, 257), (1.048, 1048)):
      self.assertEqual(CameraExposure(duration).milliseconds, milliseconds)
    for duration in (True, 0, 0.0049, 1.0481, float("inf"), float("nan")):
      with self.assertRaises(ValueError):
        CameraExposure(duration)
    with self.assertRaises(ABI7500ProtocolError):
      CameraExposure(0.256).validate_reply(b"\x01\x00")


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
  """Validate persistent idempotence, interruption handling and raw data preservation."""

  async def asyncSetUp(self) -> None:
    """Replace the transport and use a temporary journal before any method can touch hardware."""
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.usb = _CameraUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Release process ownership even when a test fails."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def test_capture_fragmentation_and_persistent_idempotence(self) -> None:
    """Raw pixels match native output; repeating the ID after reopening causes no traffic."""
    self.usb.preflight()
    self.usb.capture()
    self.usb.image(odd_fragment=True)
    self.usb.final()
    result, duplicate = await asyncio.gather(
      self.driver.capture_frame("snapshot-001", exposure=0.540),
      self.driver.capture_frame("snapshot-001", exposure=0.540),
    )
    self.assertEqual(result, duplicate)
    self.assertEqual(result.state, "complete")
    self.assertEqual(result.wire_data, _IMAGE)
    assert result.frame is not None
    self.assertEqual(
      hashlib.sha256(result.frame.image_bytes).hexdigest(),
      _CAPTURE["reference_copy"]["output_sha256"],
    )
    self.assertEqual(result.optics_before.filter_position, 1)
    self.assertEqual(CameraAcquisition.from_dict(result.to_dict()), result)
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    self.assertFalse(self.usb.script)
    await self.driver.stop()
    reopened = ABI7500FastDx(run_journal=self.path)
    reopened.io = cast(USB, self.usb)
    try:
      self.assertEqual(await reopened.capture_frame("snapshot-001", exposure=0.540), result)
      self.assertEqual(await reopened.get_acquisition("snapshot-001"), result)
      with self.assertRaisesRegex(ABI7500RunError, "different exposure"):
        await reopened.capture_frame("snapshot-001", exposure=0.180)
      self.assertEqual(self.usb.writes.count(b"CC"), 1)
    finally:
      await reopened.stop()

  async def test_exact_ubuntu_snapshot_transcript(self) -> None:
    """Replay the complete native snapshot with counted serial fragments and the actual image."""
    operations = json.loads(zlib.decompress(base64.b64decode(_SNAPSHOT["operations"])))
    for operation in operations:
      if operation[0] == "write":
        self.usb.expect_write(bytes.fromhex(operation[1]))
      else:
        self.assertEqual(operation[0], "read")
        self.usb.expect_read(bytes.fromhex(operation[1]), operation[2])
    self.driver._sequence = _SNAPSHOT["initial_thermal_sequence"]
    result = await self.driver.capture_frame("plr-snapshot-001", exposure=0.256)
    expected = _SNAPSHOT["checkpoint"]
    actual = result.to_dict()
    for key in expected:
      if key not in ("started_at", "finished_at"):
        self.assertEqual(actual[key], expected[key], key)
    self.assertEqual(hashlib.sha256(result.wire_data).hexdigest(), _SNAPSHOT["wire_sha256"])
    assert result.frame is not None
    self.assertEqual(
      hashlib.sha256(result.frame.image_bytes).hexdigest(),
      _SNAPSHOT["native_crop"]["output_sha256"],
    )
    self.assertFalse(self.usb.script)
    self.assertEqual(await self.driver.get_acquisition("plr-snapshot-001"), result)

  async def test_active_or_moving_instrument_is_rejected_before_exposure(self) -> None:
    """An active thermal sequence or a moving filter prevents CJ and CC."""
    for status, motor in ((_ACTIVE, b"\x00\x00"), (_IDLE, b"\x00\x01")):
      self.usb.preflight(status, motor)
      with self.assertRaisesRegex(ABI7500RunError, "inactive run"):
        await self.driver.capture_frame("blocked")
      self.assertFalse(self.usb.script)
    self.assertNotIn(b"CC", self.usb.writes)

  async def test_unresolved_run_and_wrong_instrument_journal_are_rejected(self) -> None:
    """A matching idle status cannot bypass unresolved run ownership."""
    prior = ThermalRun.from_dict(_HOLD["checkpoints"]["running"])
    journal = self.driver._require_journal()
    journal.reserve(prior)
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      await self.driver.capture_frame("blocked")
    journal.save(replace(prior, state="completed", camera_description="different camera"))
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "different instrument"):
      await self.driver.capture_frame("blocked")
    self.assertNotIn(b"CC", self.usb.writes)

  async def test_bad_exposure_echo_prevents_capture_and_is_not_retried(self) -> None:
    """Retain a mismatching reply and never send CC after an unconfirmed exposure setting."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2,  256")
    self.usb.expect_read(b"\x01\x00")
    with self.assertRaisesRegex(ABI7500ProtocolError, "milliseconds"):
      await self.driver.capture_frame("bad-echo")
    result = await self.driver.capture_frame("bad-echo")
    self.assertEqual(result.exposure_reply, b"\x01\x00")
    self.assertEqual(result.state, "uncertain")
    self.assertNotIn(b"CC", self.usb.writes)
    self.assertFalse(self.driver.is_connected)

  async def test_empty_image_fragment_retains_partial_transfer(self) -> None:
    """A failed CD transfer cannot return a padded or silently shortened image."""
    self.usb.preflight()
    self.usb.capture()
    self.usb.expect_read(_IMAGE[:7], 4096)
    self.usb.expect_read(b"", 4096)
    with self.assertRaisesRegex(ABI7500ProtocolError, "image fragment"):
      await self.driver.capture_frame("partial", exposure=0.540)
    result = await self.driver.get_acquisition("partial")
    self.assertEqual(result.wire_data, _IMAGE[:7])
    self.assertIsNone(result.frame)
    self.assertEqual(await self.driver.capture_frame("partial", exposure=0.540), result)
    self.assertEqual(self.usb.writes.count(b"CC"), 1)

  async def test_changed_final_status_retains_full_frame_as_uncertain(self) -> None:
    """A complete byte transfer alone does not establish a coherent idle snapshot."""
    self.usb.preflight()
    self.usb.capture()
    self.usb.image()
    self.usb.final(_ACTIVE)
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.driver.capture_frame("changed", exposure=0.540)
    result = await self.driver.get_acquisition("changed")
    self.assertEqual(result.wire_data, _IMAGE)
    self.assertEqual(result.state, "uncertain")
    self.assertIsNotNone(result.frame)

  async def test_cancel_during_poll_preserves_identity_and_never_resends_cc(self) -> None:
    """Task cancellation records uncertainty without sending a guessed abort sequence."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2,  256")
    self.usb.expect_read(b"\x00\x01")
    self.usb.expect_write(b"CC")
    with patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep",
      new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
      with self.assertRaises(asyncio.CancelledError):
        await self.driver.capture_frame("cancelled")
    self.assertEqual((await self.driver.capture_frame("cancelled")).state, "uncertain")
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    self.assertNotIn(b"Cc", self.usb.writes)

  async def test_unknown_camera_state_faults_connection(self) -> None:
    """Unrecognized replies do not trigger CD or permit subsequent I/O."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2,  256")
    self.usb.expect_read(b"\x00\x01")
    self.usb.expect_write(b"CC")
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"error")
    with self.assertRaisesRegex(ABI7500ProtocolError, "readiness"):
      await self.driver.capture_frame("error")
    self.assertFalse(self.driver.is_connected)
    with self.assertRaisesRegex(RuntimeError, "communication failed"):
      await self.driver.capture_frame("another")
    self.assertFalse(self.usb.script)

  async def test_readiness_uses_one_deadline(self) -> None:
    """A timeout after CC persists uncertainty and does not issue a second exposure."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2,    5")
    self.usb.expect_read(b"\x05\x00")
    self.usb.expect_write(b"CC")

    async def expire(_: float) -> None:
      """Advance real time beyond the exposure's shared readiness deadline."""
      await _SLEEP(0.03)

    # Give preflight normal deadlines and reduce the deadline only after CC is written.
    write = self.usb.write

    async def write_then_shorten(data: bytes, timeout: float) -> None:
      """Shorten only the readiness window, leaving preflight unrelated to test timing."""
      await write(data, timeout)
      if data == b"CC":
        self.driver.command_timeout = 0.001

    with (
      patch.object(self.usb, "write", side_effect=write_then_shorten),
      patch("pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", side_effect=expire),
    ):
      with self.assertRaises(TimeoutError):
        await self.driver.capture_frame("timed-out", exposure=0.005)
    result = await self.driver.get_acquisition("timed-out")
    self.assertEqual(result.state, "uncertain")
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    self.assertFalse(self.usb.script)

  async def test_ambiguous_cc_write_has_durable_intent_before_dispatch(self) -> None:
    """A timeout from a submitted CC cannot erase the durable ID or cause a repeated CC."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2,  256")
    self.usb.expect_read(b"\x00\x01")
    self.usb.expect_write(b"CC")
    write = self.usb.write

    async def uncertain_write(data: bytes, timeout: float) -> None:
      """Observe the committed record from a second database connection before the CC write."""
      if data == b"CC":
        with sqlite3.connect(self.path) as db:
          row = db.execute("SELECT record FROM acquisitions").fetchone()
        assert row is not None
        record = CameraAcquisition.from_dict(json.loads(row[0]))
        self.assertEqual(record.acquisition_id, "write-timeout")
        self.assertEqual(record.state, "uncertain")
      await write(data, timeout)
      if data == b"CC":
        raise TimeoutError("ambiguous submitted write")

    with patch.object(self.usb, "write", side_effect=uncertain_write):
      with self.assertRaises(TimeoutError):
        await self.driver.capture_frame("write-timeout")
    await self.driver.stop()
    self.assertEqual((await self.driver.capture_frame("write-timeout")).state, "uncertain")
    self.assertEqual(self.usb.writes.count(b"CC"), 1)

  async def test_long_exposure_keeps_the_captured_polling_sequence(self) -> None:
    """Busy polling excludes idle keepalives even across a normal heartbeat interval."""
    self.usb.preflight()
    self.usb.expect_write(b"CJ   2, 1048")
    self.usb.expect_read(b"\x18\x04")
    self.usb.expect_write(b"CC")
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"not r")
    self.usb.expect_write(b"CH   5")
    self.usb.expect_read(b"ready")
    self.usb.expect_write(b"CD  64,2458 ")
    self.usb.image()
    self.usb.final()

    async def delay_first_poll(_: float) -> None:
      """Wait across one heartbeat interval only before the first readiness query."""
      if self.usb.writes[-1] == b"CC":
        await _SLEEP(0.51)

    with patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", side_effect=delay_first_poll
    ):
      result = await self.driver.capture_frame("keepalive", exposure=1.048)
    self.assertEqual(result.state, "complete")
    self.assertNotIn(b"C:", self.usb.writes)
    self.assertFalse(self.usb.script)
