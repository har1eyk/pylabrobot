"""Exercise USB transactions and connection failure handling without opening hardware."""

import asyncio
import json
import unittest
from collections import deque
from pathlib import Path
from typing import Optional, cast
from unittest.mock import AsyncMock

from pylabrobot.io.usb import USB

from .driver import ABI7500DeviceError, ABI7500FastDx
from .protocol import ABI7500ProtocolError, ThermalPacket, encode_serial_read, encode_serial_write

_DATA = Path(__file__).parent / "test_data"
_IDENTITY = json.loads((_DATA / "identity_vector.json").read_text())
_CAMERA = bytes.fromhex(_IDENTITY["camera_hex"])
_VERSION = bytes.fromhex(_IDENTITY["thermal"]["reply"]["payload_hex"])
_STATUS = bytes.fromhex(
  json.loads((_DATA / "status_vectors.json").read_text())["vectors"][0]["reply"]["payload_hex"]
)


class _ScriptedUSB:
  """Strict ordered fake: every host write and read size must match the script."""

  def __init__(self) -> None:
    """Create an empty transaction script and lifecycle mocks."""
    self.script: deque[tuple[str, bytes, int]] = deque()
    self.writes: list[bytes] = []
    self.setup = AsyncMock()
    self.stop = AsyncMock()
    self.read_started = asyncio.Event()
    self.release_read: Optional[asyncio.Event] = None
    self.read_error: Optional[Exception] = None

  def expect_write(self, data: bytes) -> None:
    """Append one required host transfer."""
    self.script.append(("write", data, len(data)))

  def expect_read(self, data: bytes, requested_size: Optional[int] = None) -> None:
    """Append one response, optionally shorter than the host's requested buffer."""
    self.script.append(("read", data, len(data) if requested_size is None else requested_size))

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Validate write ordering and exact bytes."""
    assert self.script, f"Unexpected write {data!r}"
    assert self.script.popleft() == ("write", data, len(data))
    self.writes.append(data)

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Return the next scripted response or injected transport failure."""
    self.read_started.set()
    if self.release_read is not None:
      await self.release_read.wait()
    if self.read_error is not None:
      raise self.read_error
    assert self.script, "Unexpected read"
    action, data, expected_size = self.script.popleft()
    assert (action, expected_size) == ("read", size)
    return data

  def query(
    self,
    command: int,
    sequence: int,
    payload: bytes,
    expected_size: Optional[int] = None,
    error: int = 0,
    reply_sequence: Optional[int] = None,
  ) -> None:
    """Script a query with a synthetic envelope around captured payload bytes."""
    for chunk in encode_serial_write(ThermalPacket(command, sequence).encode()):
      self.expect_write(chunk)
    reply = ThermalPacket(
      command, sequence if reply_sequence is None else reply_sequence, payload, error_code=error
    ).encode()
    capacity = (len(payload) if expected_size is None else expected_size) + 21
    self.expect_write(encode_serial_read(capacity))
    self.expect_read(len(reply).to_bytes(2, "little") + reply + bytes(capacity - len(reply)))

  def connection(self, boot: bytes = b"APPL" + bytes(6)) -> None:
    """Script the driver's subset of the captured initialization handshake."""
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")
    self.expect_write(b"Cf  15,,.,1.05,2.19,,")
    self.expect_write(b"CF  64")
    self.expect_read(_CAMERA)
    self.expect_write(b"CI   4")
    self.expect_read(bytes.fromhex(_IDENTITY["geometry_hex"]))
    self.expect_write(b"RP  109600,8,0,0")
    self.expect_write(b"RC")
    self.expect_write(b"RP  109600,8,0,0")
    self.query(0x2F, 1, boot)
    if boot.startswith(b"APPL\x00"):
      self.query(0x0A, 2, _VERSION)


class DriverTests(unittest.IsolatedAsyncioTestCase):
  """Verify bounded transactions, serialization and fail-closed recovery behavior."""

  async def asyncSetUp(self) -> None:
    """Replace USB before any lifecycle method can run."""
    self.driver = ABI7500FastDx(command_timeout=0.2)
    self.usb = _ScriptedUSB()
    self.driver.io = cast(USB, self.usb)

  async def asyncTearDown(self) -> None:
    """Always stop background tasks, including after an assertion fails."""
    if self.usb.release_read is not None:
      self.usb.release_read.set()
    await self.driver.stop()

  async def test_setup_queries_and_idempotent_lifecycle(self) -> None:
    """Setup/query/stop require no heating, program-loading or filter-moving commands."""
    self.usb.connection()
    await self.driver.setup()
    await self.driver.setup()
    self.usb.setup.assert_awaited_once_with(empty_buffer=False)
    self.assertTrue(self.driver.is_connected)
    self.assertEqual(self.driver.camera_geometry.wire_width, 321)
    self.usb.query(0x29, 3, _STATUS)
    self.assertEqual((await self.driver.get_status()).raw_payload, _STATUS)
    self.usb.expect_write(b"CF  64")
    self.usb.expect_read(_CAMERA)
    self.usb.query(0x0A, 4, _VERSION)
    self.assertEqual((await self.driver.get_identity()).thermal_version, "G2.10")
    self.assertFalse(self.usb.script)
    await self.driver.stop()
    await self.driver.stop()
    self.assertFalse(self.driver.is_connected)

  async def test_bootloader_is_rejected_without_firmware_commands(self) -> None:
    """A non-application response closes USB instead of attempting a firmware download."""
    self.usb.connection(b"BOOT" + bytes(6))
    with self.assertRaisesRegex(ABI7500ProtocolError, "application mode"):
      await self.driver.setup()
    self.usb.stop.assert_awaited_once()
    self.assertFalse(self.driver.is_connected)
    self.assertFalse(self.usb.script)

  async def test_fragmented_counted_reply_and_nonzero_padding(self) -> None:
    """Use real version fragments and padding from the connection capture."""
    fixture = json.loads((_DATA / "usb_serial_vectors.json").read_text())
    for vector in fixture["exchanges"]:
      if not vector["name"].startswith("version"):
        continue
      request = ThermalPacket.decode(bytes.fromhex(vector["thermal_request_hex"]))
      self.driver._sequence = request.sequence - 1
      self.driver._connected = True
      self.usb.expect_write(b"CF  64")
      self.usb.expect_read(_CAMERA[:7], 64)
      self.usb.expect_read(_CAMERA[7:], 57)
      self.usb.expect_write(bytes.fromhex(vector["write_hex"]))
      for read in vector["serial_reads"]:
        self.usb.expect_write(bytes.fromhex(read["request_hex"]))
        wire = bytes.fromhex(read["reply_hex"])
        self.usb.expect_read(wire[:1], len(wire))
        self.usb.expect_read(wire[1:], len(wire) - 1)
      identity = await self.driver.get_identity()
      expected = ThermalPacket.decode(bytes.fromhex(vector["thermal_reply_hex"]))
      self.assertEqual(identity.thermal_response, expected.payload)
      self.assertFalse(self.usb.script)

  async def test_empty_serial_reads_use_one_command_deadline(self) -> None:
    """An empty valid-byte count with a nonzero tail is not a thermal reply."""
    self.driver._connected = True
    self.driver.command_timeout = 0.025
    self.usb.expect_write(encode_serial_write(ThermalPacket(0x29, 1).encode())[0])
    for _ in range(10):
      self.usb.expect_write(encode_serial_read(71))
      self.usb.expect_read(b"\x00\x00" + b"\xff" * 71)
    with self.assertRaises(TimeoutError):
      await self.driver.get_status()
    self.assertEqual(sum(chunk.startswith(b"RS") for chunk in self.usb.writes), 1)
    self.assertFalse(self.driver.is_connected)

  async def test_ubuntu_hardware_transcript(self) -> None:
    """Replay real boot/status polling with short serial fragments and stale padded tails."""
    fixture = json.loads((_DATA / "ubuntu_observation_vectors.json").read_text())
    self.driver.command_timeout = 1.0
    self.driver._connected = True
    for exchange in fixture["exchanges"]:
      with self.subTest(command=exchange["command"]):
        self.driver._sequence = exchange["sequence"] - 1
        for transfer in exchange["transfers"]:
          data = bytes.fromhex(transfer["hex"])
          if transfer["action"] == "write":
            self.usb.expect_write(data)
          else:
            self.assertEqual(transfer["action"], "read")
            self.usb.expect_read(data)
        expected = bytes.fromhex(exchange["payload_hex"])
        if exchange["command"] == 0x29:
          self.assertEqual((await self.driver.get_status()).raw_payload, expected)
        else:
          self.assertEqual(await self.driver._thermal_query(0x2F, 10), expected)
        self.assertFalse(self.usb.script)

  async def test_concurrent_queries_are_serialized(self) -> None:
    """A second caller cannot consume the first caller's fragmented response."""
    self.driver._connected = True
    self.usb.query(0x29, 1, _STATUS)
    self.usb.query(0x29, 2, _STATUS)
    results = await asyncio.gather(self.driver.get_status(), self.driver.get_status())
    self.assertEqual([result.raw_payload for result in results], [_STATUS, _STATUS])
    self.assertFalse(self.usb.script)

  async def test_transport_failure_blocks_following_queries_without_resend(self) -> None:
    """An ambiguous timeout requires closing and reopening; no implicit query replay occurs."""
    self.driver._connected = True
    self.usb.query(0x29, 1, _STATUS)
    self.usb.read_error = TimeoutError("injected timeout")
    with self.assertRaises(TimeoutError):
      await self.driver.get_status()
    writes = list(self.usb.writes)
    for method in (self.driver.get_status, self.driver.get_identity, self.driver.setup):
      with self.assertRaisesRegex(RuntimeError, "communication failed"):
        await method()
    self.assertEqual(self.usb.writes, writes)
    self.assertFalse(self.driver.is_connected)

  async def test_short_error_reply_preserves_firmware_error(self) -> None:
    """A valid error envelope may be shorter than the successful response."""
    self.driver._connected = True
    self.usb.query(0x29, 1, b"", expected_size=50, error=0x76)
    with self.assertRaises(ABI7500DeviceError) as caught:
      await self.driver.get_status()
    self.assertEqual(caught.exception.reply.error_code, 0x76)
    self.assertFalse(self.usb.script)

  async def test_stale_reply_does_not_satisfy_new_query(self) -> None:
    """A correct checksum is insufficient when the reply sequence belongs to another request."""
    self.driver._connected = True
    self.usb.query(0x29, 1, _STATUS, reply_sequence=99)
    with self.assertRaisesRegex(ABI7500ProtocolError, "pending query"):
      await self.driver.get_status()
    self.assertFalse(self.driver.is_connected)

  async def test_cancellation_finishes_inflight_io_before_closing(self) -> None:
    """Cancelled executor-backed reads must finish before teardown can dispose the USB handle."""
    self.driver._connected = True
    self.usb.query(0x29, 1, _STATUS)
    self.usb.release_read = asyncio.Event()
    query = asyncio.create_task(self.driver.get_status())
    await self.usb.read_started.wait()
    query.cancel()
    closing = asyncio.create_task(self.driver.stop())
    await asyncio.sleep(0)
    self.assertFalse(query.done())
    self.usb.stop.assert_not_awaited()
    query.cancel()
    await asyncio.sleep(0)
    self.assertFalse(query.done())
    self.usb.stop.assert_not_awaited()
    self.usb.release_read.set()
    with self.assertRaises(asyncio.CancelledError):
      await query
    await closing
    self.usb.stop.assert_awaited_once()

  async def test_keepalive_failure_is_visible_to_next_operation(self) -> None:
    """Background failures must not leave a healthy-looking connection."""
    self.usb.connection()
    await self.driver.setup()
    self.usb.expect_write(b"C:")
    # A second heartbeat will fail because the fake allows only the explicitly scripted write.
    await asyncio.sleep(1.05)
    self.assertFalse(self.driver.is_connected)
    with self.assertRaisesRegex(RuntimeError, "communication failed"):
      await self.driver.get_status()
