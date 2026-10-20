"""Optical control framing, desired-state reconciliation and interrupted operations."""

import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .acquisition import OpticalState
from .acquisition_tests import _ACTIVE, _HOLD, _IDLE
from .driver import ABI7500DeviceError, ABI7500FastDx
from .driver_tests import _CAMERA, _VERSION, _ScriptedUSB
from .protocol import ABI7500ProtocolError, ThermalPacket, encode_serial_read, encode_serial_write
from .run import ABI7500RunError, ThermalRun

_VECTORS = json.loads(
  (Path(__file__).parent / "test_data/optical_control_vectors.json").read_text()
)["vectors"]
_UBUNTU = json.loads((Path(__file__).parent / "test_data/ubuntu_optics_vector.json").read_text())


class _OpticalUSB(_ScriptedUSB):
  """Strict scripts covering optical commands under the shared USB transaction lock."""

  def __init__(self) -> None:
    """Start with no real transport and an independent sequence counter."""
    super().__init__()
    self.sequence = 0
    self.commands: list[tuple[int, bytes]] = []

  def exchange(self, command: int, reply: bytes, payload: bytes = b"", error: int = 0) -> None:
    """Queue a command and count-prefixed reply with exact outgoing payload bytes."""
    self.sequence += 1
    self.commands.append((command, payload))
    for chunk in encode_serial_write(ThermalPacket(command, self.sequence, payload).encode()):
      self.expect_write(chunk)
    packet = ThermalPacket(command, self.sequence, reply, error_code=error).encode()
    self.expect_write(encode_serial_read(len(packet)))
    self.expect_read(len(packet).to_bytes(2, "little") + packet)

  def optics(self, position: int = 1, lamp: bytes = b"\x00\x01\x00\x00", motor: int = 0) -> None:
    """Queue all three original optical replies."""
    self.exchange(0x6C, motor.to_bytes(2, "big"))
    self.exchange(0x6B, position.to_bytes(2, "big"))
    self.exchange(0x66, lamp)

  def preflight(
    self,
    position: int = 1,
    lamp: bytes = b"\x00\x01\x00\x00",
    motor: int = 0,
    status: bytes = _IDLE,
    camera: bytes = _CAMERA,
    version: bytes = _VERSION,
  ) -> None:
    """Queue identity and coherent idle checks without any optical mutations."""
    self.expect_write(b"CF  64")
    self.expect_read(camera)
    self.exchange(0x0A, version)
    self.exchange(0x29, status)
    self.optics(position, lamp, motor)
    self.exchange(0x29, status)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")

  def final(
    self, position: int = 1, lamp: bytes = b"\x00\x01\x00\x00", status: bytes = _IDLE
  ) -> None:
    """Queue post-control optical and thermal readback."""
    self.optics(position, lamp)
    self.exchange(0x29, status)


class OpticalControlTests(unittest.IsolatedAsyncioTestCase):
  """Actuation never bypasses preflight or repeats automatically after an ambiguous failure."""

  async def asyncSetUp(self) -> None:
    """Replace USB before any driver method can open hardware."""
    self.temp = tempfile.TemporaryDirectory()
    self.driver = ABI7500FastDx(run_journal=Path(self.temp.name) / "runs.sqlite3")
    self.usb = _OpticalUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Close the isolated journal and ensure no real transport survives a test."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def test_recovered_frames(self) -> None:
    """Exact captured packets establish command numbers, BE payloads, error fields and checksums."""
    for pair in _VECTORS:
      request, reply = pair["request"], pair["reply"]
      packet = ThermalPacket.decode(bytes.fromhex(request["packet_hex"]))
      self.assertEqual(packet.command, request["command"])
      self.assertEqual(packet.payload.hex(), request["payload_hex"])
      for chunk in encode_serial_write(bytes.fromhex(request["packet_hex"])):
        self.usb.expect_write(chunk)
      raw = bytes.fromhex(reply["packet_hex"])
      self.usb.expect_write(encode_serial_read(len(raw)))
      self.usb.expect_read(len(raw).to_bytes(2, "little") + raw)
      self.driver._sequence = packet.sequence - 1
      response = await self.driver._exchange(packet.command, len(raw) - 21, packet.payload)
      self.assertEqual(response.hex(), reply["payload_hex"])
    self.assertFalse(self.usb.script)

  async def test_ubuntu_optical_session(self) -> None:
    """Replay actual filter/lamp mutations, duplicate requests, one exposure and restoration."""
    operations = json.loads(zlib.decompress(base64.b64decode(_UBUNTU["operations"])))
    for operation in operations:
      if operation[0] == "write":
        self.usb.expect_write(bytes.fromhex(operation[1]))
      else:
        self.assertEqual(operation[0], "read")
        self.usb.expect_read(bytes.fromhex(operation[1]), operation[2])
    self.driver._sequence = _UBUNTU["initial_thermal_sequence"]
    for _ in range(2):
      self.assertEqual((await self.driver.set_filter_position(1)).filter_position, 1)
    for _ in range(2):
      self.assertTrue((await self.driver.set_lamp(True, power=0)).lamp_enabled)
    acquisition = await self.driver.capture_frame("plr-optics-001-f1", exposure=0.256)
    self.assertEqual(acquisition.state, "complete")
    self.assertEqual(hashlib.sha256(acquisition.wire_data).hexdigest(), _UBUNTU["wire_sha256"])
    assert acquisition.frame is not None
    self.assertEqual(
      hashlib.sha256(acquisition.frame.image_bytes).hexdigest(),
      _UBUNTU["native_crop"]["output_sha256"],
    )
    before = list(self.usb.script)
    self.assertEqual(
      await self.driver.capture_frame("plr-optics-001-f1", exposure=0.256), acquisition
    )
    self.assertEqual(list(self.usb.script), before)
    self.assertFalse((await self.driver.set_lamp(False)).lamp_enabled)
    self.assertEqual((await self.driver.set_filter_position(0)).to_dict(), _UBUNTU["final_optics"])
    self.assertEqual((await self.driver.get_status()).phase, "inactive")
    self.assertFalse(self.usb.script)

  async def test_filter_move_busy_poll_and_duplicate(self) -> None:
    """Wait through motion and confirm position; repeated desired positions send no move."""
    self.usb.preflight()
    self.usb.exchange(0x69, b"", b"\x00\x05")
    self.usb.exchange(0x6C, b"\x00\x01")
    self.usb.exchange(0x6C, b"\x00\x00")
    self.usb.final(5)
    self.assertEqual((await self.driver.set_filter_position(5)).filter_position, 5)
    self.usb.preflight(5)
    self.usb.final(5)
    await self.driver.set_filter_position(5)
    self.assertEqual(self.usb.commands.count((0x69, b"\x00\x05")), 1)
    self.assertFalse(self.usb.script)

  async def test_existing_motion_is_reconciled_before_deciding_to_move(self) -> None:
    """The initial position can be stale while a previous move is finishing."""
    self.usb.preflight(1, motor=1)
    self.usb.exchange(0x6C, b"\x00\x00")
    self.usb.final(5)
    self.usb.final(5)
    await self.driver.set_filter_position(5)
    self.assertFalse(any(cmd == 0x69 for cmd, _ in self.usb.commands))
    self.assertFalse(self.usb.script)

  async def test_enable_power_change_disable_and_duplicates(self) -> None:
    """Follow vendor enable-before-power ordering and skip writes satisfied by actual readback."""
    off = bytes.fromhex("00000002")
    on = bytes.fromhex("00010000")
    standby = bytes.fromhex("00010001")
    self.usb.preflight(lamp=off)
    self.usb.exchange(0x64, b"", b"\x00\x01")
    self.usb.exchange(0x65, b"", b"\x00\x00")
    self.usb.final(lamp=on)
    await self.driver.set_lamp(True, power=0)
    self.usb.preflight(lamp=on)
    self.usb.final(lamp=on)
    await self.driver.set_lamp(True, power=0)
    self.usb.preflight(lamp=on)
    self.usb.exchange(0x65, b"", b"\x00\x01")
    self.usb.final(lamp=standby)
    await self.driver.set_lamp(True, power=1)
    self.usb.preflight(lamp=standby)
    self.usb.exchange(0x64, b"", b"\x00\x00")
    self.usb.final(lamp=off)
    await self.driver.set_lamp(False)
    self.usb.preflight(lamp=off)
    self.usb.final(lamp=off)
    await self.driver.set_lamp(False)
    mutations = [(cmd, data.hex()) for cmd, data in self.usb.commands if cmd in (0x64, 0x65)]
    self.assertEqual(mutations, [(0x64, "0001"), (0x65, "0000"), (0x65, "0001"), (0x64, "0000")])
    self.assertFalse(self.usb.script)

  async def test_active_and_moving_lamp_preflight(self) -> None:
    """No optical mutation is allowed during a thermal run or exposure/motor conflict."""
    self.usb.preflight(status=_ACTIVE)
    with self.assertRaisesRegex(ABI7500RunError, "inactive"):
      await self.driver.set_filter_position(2)
    self.usb.preflight(motor=1)
    with self.assertRaisesRegex(ABI7500RunError, "stationary"):
      await self.driver.set_lamp(True, power=0)
    self.assertFalse(any(cmd in (0x64, 0x65, 0x69) for cmd, _ in self.usb.commands))

  async def test_lamp_delays_surround_power_command(self) -> None:
    """The vendor's enable delay and post-mutation delay must precede power and readback."""
    events: list[tuple[str, float]] = []

    async def query(command: int, *args: object) -> bytes:
      """Record mutations independently of the transport framing tests."""
      events.append(("command", command))
      return b""

    async def sleep(duration: float) -> None:
      """Record the requested settling time without wall-clock waits."""
      events.append(("sleep", duration))

    async def finish(*args: object) -> OpticalState:
      """Mark the first readback after mutation."""
      events.append(("readback", 0))
      return OpticalState(b"\x00\x01", b"\x00\x00", bytes.fromhex("00010000"))

    off = OpticalState(b"\x00\x01", b"\x00\x00", bytes.fromhex("00000002"))
    with (
      patch.object(self.driver, "_optical_preflight", AsyncMock(return_value=(None, off))),
      patch.object(self.driver, "_thermal_query", query),
      patch.object(self.driver, "_finish_optical_control", finish),
      patch("pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", sleep),
    ):
      await self.driver.set_lamp(True, power=0)
    self.assertEqual(
      events,
      [("command", 0x64), ("sleep", 0.25), ("command", 0x65), ("sleep", 0.25), ("readback", 0)],
    )

  async def test_unresolved_run_and_other_instrument_journal(self) -> None:
    """Current idle status alone does not establish ownership of a previously ambiguous run."""
    journal = self.driver._require_journal()
    run = ThermalRun.from_dict(_HOLD["checkpoints"]["running"])
    journal.reserve(run)
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "Resolve run"):
      await self.driver.set_lamp(False)
    journal.save(replace(run, state="completed", camera_description="other"))
    self.usb.preflight()
    with self.assertRaisesRegex(ABI7500RunError, "different instrument"):
      await self.driver.set_filter_position(2)
    self.assertFalse(self.usb.script)

  async def test_errors_and_mismatched_readback_fault_without_retry(self) -> None:
    """A negative acknowledgement prevents further commands, including implicit cleanup moves."""
    self.usb.preflight()
    self.usb.exchange(0x69, b"", b"\x00\x02", error=74)
    with self.assertRaises(ABI7500DeviceError):
      await self.driver.set_filter_position(2)
    with self.assertRaisesRegex(RuntimeError, "communication failed"):
      await self.driver.set_filter_position(2)
    self.assertFalse(self.usb.script)
    self.driver._fault = None
    self.usb.preflight()
    self.usb.exchange(0x65, b"", b"\x00\x01")
    self.usb.final()
    with self.assertRaisesRegex(ABI7500ProtocolError, "readback"):
      await self.driver.set_lamp(True, power=1)
    self.assertFalse(self.driver.is_connected)
    self.assertFalse(self.usb.script)

  async def test_timeout_and_task_cancellation_after_move_never_repeat(self) -> None:
    """Both ambiguous failure classes require reconnect and actual-state reconciliation."""
    for error in (TimeoutError("lost response"), asyncio.CancelledError()):
      self.driver._fault = None
      self.usb = _OpticalUSB()
      self.driver.io = cast(USB, self.usb)
      self.driver._sequence = 0
      self.usb.preflight()
      original = self.driver._thermal_query

      async def query(command: int, size: int, payload: bytes = b"") -> bytes:
        """Inject an ambiguous failure at the requested mutation."""
        if command == 0x69:
          raise error
        return await original(command, size, payload)

      with patch.object(self.driver, "_thermal_query", side_effect=query) as mock:
        with self.assertRaises(type(error)):
          await self.driver.set_filter_position(2)
        self.assertEqual(sum(call.args[0] == 0x69 for call in mock.call_args_list), 1)
      self.assertFalse(self.driver.is_connected)
      self.assertFalse(self.usb.script)

  async def test_invalid_arguments_and_unknown_status(self) -> None:
    """Bad arguments and unknown lamp words cannot become actuation commands."""
    for position in (-1, 6, True, 1.0):
      with self.assertRaises(ValueError):
        await self.driver.set_filter_position(cast(int, position))
    for enabled, power in ((1, 0), (True, None), (True, 2), (True, True), (False, 0)):
      with self.assertRaises(ValueError):
        await self.driver.set_lamp(cast(bool, enabled), power=power)
    self.assertFalse(self.usb.writes)
    self.usb.preflight(lamp=bytes.fromhex("00020000"))
    with self.assertRaisesRegex(ABI7500ProtocolError, "enable"):
      await self.driver.set_lamp(False)
    self.assertFalse(self.usb.script)

  async def test_concurrent_setters_do_not_interleave(self) -> None:
    """A lamp request cannot split the filter's command/readback transaction."""
    self.usb.preflight()
    self.usb.exchange(0x69, b"", b"\x00\x02")
    self.usb.exchange(0x6C, b"\x00\x00")
    self.usb.final(2)
    self.usb.preflight(2)
    self.usb.exchange(0x65, b"", b"\x00\x01")
    self.usb.final(2, bytes.fromhex("00010001"))
    moved, lamp = await asyncio.gather(
      self.driver.set_filter_position(2), self.driver.set_lamp(True, power=1)
    )
    self.assertEqual(moved.filter_position, 2)
    self.assertEqual(lamp.lamp_power, 1)
    self.assertFalse(self.usb.script)

  async def test_state_changes_while_waiting_prevent_another_move(self) -> None:
    """An intervening thermal start cannot be ignored while waiting for old filter motion."""
    self.usb.preflight(1, motor=1)
    self.usb.exchange(0x6C, b"\x00\x00")
    self.usb.final(2, status=_ACTIVE)
    with self.assertRaisesRegex(ABI7500RunError, "state changed"):
      await self.driver.set_filter_position(5)
    self.assertFalse(any(cmd == 0x69 for cmd, _ in self.usb.commands))
    self.assertFalse(self.usb.script)

  async def test_read_only_optical_state(self) -> None:
    """Readback retains raw bytes and recognizes off without inferring lamp brightness."""
    self.usb.optics(0, bytes.fromhex("00000002"))
    optics = await self.driver.get_optical_state()
    self.assertFalse(optics.lamp_enabled)
    self.assertEqual(optics.lamp_power, 2)
    self.assertEqual(OpticalState.from_dict(optics.to_dict()), optics)
    self.assertFalse(self.usb.script)
