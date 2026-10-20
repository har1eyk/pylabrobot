"""Check stored exposure decoding and read-only queries without opening hardware."""

import base64
import hashlib
import json
import unittest
import zlib
from pathlib import Path
from typing import cast

from pylabrobot.io.usb import USB

from .acquisition import StoredExposureTimes
from .driver import ABI7500DeviceError, ABI7500FastDx
from .driver_tests import _ScriptedUSB
from .protocol import ABI7500ProtocolError, ThermalPacket

_FIXTURE = json.loads(
  (Path(__file__).parent / "test_data/stored_exposure_vectors.json").read_text()
)
_PAYLOAD = ThermalPacket.decode(bytes.fromhex(_FIXTURE["exchanges"][0]["reply_hex"])).payload


class StoredExposureTimesTests(unittest.TestCase):
  """Verify physical order, units and preservation of uninterpreted data."""

  def test_captured_order_and_seconds(self) -> None:
    """Keep all five physical filters and the three exposure slots in recorded order."""
    for exchange in _FIXTURE["exchanges"]:
      table = StoredExposureTimes(
        ThermalPacket.decode(bytes.fromhex(exchange["reply_hex"])).payload
      )
      for filter, expected in enumerate(_FIXTURE["expected_seconds"], 1):
        self.assertEqual(table.for_filter(filter), tuple(expected))

  def test_preserves_opaque_bytes_and_reported_values(self) -> None:
    """Do not infer a header format or clamp a stored value into a supported request range."""
    data = b"\xff\x81" + b"\xff\xff\xff\xff\x7f\xff\xff\xff" + bytes(52) + bytes(range(192))
    table = StoredExposureTimes(data)
    self.assertEqual(table.raw_payload, data)
    self.assertEqual(table.for_filter(1), (-0.001, 2147483.647, 0.0))
    self.assertEqual(table.for_filter(5), (0.0, 0.0, 0.0))

  def test_incomplete_extra_and_mutable_payloads_rejected(self) -> None:
    """Never decode partial tables or retain caller-owned mutable bytes."""
    for data in (b"", _PAYLOAD[:-1], _PAYLOAD + b"\x00", bytearray(_PAYLOAD), "x" * 254):
      with self.subTest(type=type(data).__name__, length=len(data)):
        with self.assertRaises(ABI7500ProtocolError):
          StoredExposureTimes(cast(bytes, data))

  def test_filter_bounds_and_type(self) -> None:
    """Reject parked position zero, out-of-range positions and ambiguous index types."""
    table = StoredExposureTimes(_PAYLOAD)
    for value in (-1, 0, 6, True, 1.0, "1"):
      with self.subTest(value=value):
        with self.assertRaises(ValueError):
          table.for_filter(cast(int, value))


class StoredExposureQueryTests(unittest.IsolatedAsyncioTestCase):
  """Verify captured fragmented transactions and fail-closed error handling."""

  async def asyncSetUp(self) -> None:
    """Install the strict fake before any connection or query can execute."""
    self.driver = ABI7500FastDx(command_timeout=0.2)
    self.usb = _ScriptedUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True

  async def asyncTearDown(self) -> None:
    """Close only the fake transport."""
    await self.driver.stop()

  async def test_original_fragmented_usb_transactions(self) -> None:
    """Replay two full vendor exchanges, retaining counted-read padding and fragments."""
    for exchange in _FIXTURE["exchanges"]:
      self.driver._sequence = exchange["sequence"] - 1
      for transfer in exchange["transfers"]:
        data = bytes.fromhex(transfer["hex"])
        if transfer["action"] == "write":
          self.usb.expect_write(data)
        else:
          self.assertEqual(transfer["action"], "read")
          self.usb.expect_read(data)
      table = await self.driver.get_stored_exposure_times()
      self.assertEqual(table.raw_payload, _PAYLOAD)
      self.assertEqual(table.for_filter(5), (1.048, 0.09, 0.03))
      self.assertFalse(self.usb.script)

  async def test_repeated_queries_return_fresh_readback(self) -> None:
    """Read again without a journal, cached result or any table-write command."""
    changed = _PAYLOAD[:2] + (42).to_bytes(4, "big") + _PAYLOAD[6:]
    self.usb.query(0x80, 1, _PAYLOAD)
    self.usb.query(0x80, 2, changed)
    self.assertEqual((await self.driver.get_stored_exposure_times()).for_filter(1)[0], 0.54)
    self.assertEqual((await self.driver.get_stored_exposure_times()).for_filter(1)[0], 0.042)
    self.assertFalse(self.usb.script)

  async def test_ubuntu_fragmented_usb_transaction(self) -> None:
    """Replay all 142 counted reads, including empty reads and stale nonzero padding."""
    fixture = json.loads(
      (Path(__file__).parent / "test_data/ubuntu_stored_exposure_vectors.json").read_text()
    )
    operations = zlib.decompress(base64.b64decode(fixture["operations"]))
    self.assertEqual(hashlib.sha256(operations).hexdigest(), fixture["operations_sha256"])
    self.driver = ABI7500FastDx(command_timeout=5)
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.driver._sequence = fixture["sequence"] - 1
    for transfer in json.loads(operations):
      data = bytes.fromhex(transfer["data_hex"])
      if transfer["operation"] == "write":
        self.usb.expect_write(data)
      else:
        self.assertEqual(transfer["operation"], "read")
        self.usb.expect_read(data)
    table = await self.driver.get_stored_exposure_times()
    self.assertEqual(table.raw_payload, _PAYLOAD)
    for filter, expected in enumerate(_FIXTURE["expected_seconds"], 1):
      self.assertEqual(table.for_filter(filter), tuple(expected))
    self.assertFalse(self.usb.script)

  async def test_short_payload_faults_connection(self) -> None:
    """A complete thermal reply with an incomplete table cannot yield exposure values."""
    self.usb.query(0x80, 1, _PAYLOAD[:-1], expected_size=254)
    with self.assertRaisesRegex(ABI7500ProtocolError, "payload size"):
      await self.driver.get_stored_exposure_times()
    self.assertFalse(self.driver.is_connected)
    self.assertFalse(self.usb.script)

  async def test_device_error_preserves_error_and_does_not_retry(self) -> None:
    """Keep the device error separate from exposure data; never replay the query."""
    self.usb.query(0x80, 1, b"", expected_size=254, error=0x1234)
    with self.assertRaises(ABI7500DeviceError) as caught:
      await self.driver.get_stored_exposure_times()
    self.assertEqual(caught.exception.reply.error_code, 0x1234)
    self.assertFalse(self.driver.is_connected)
    self.assertFalse(self.usb.script)
    with self.assertRaisesRegex(RuntimeError, "communication failed"):
      await self.driver.get_stored_exposure_times()

  async def test_timeout_faults_without_retry(self) -> None:
    """An interrupted read prevents another query from consuming a late response."""
    self.usb.query(0x80, 1, _PAYLOAD)
    self.usb.read_error = TimeoutError("injected timeout")
    with self.assertRaises(TimeoutError):
      await self.driver.get_stored_exposure_times()
    writes = list(self.usb.writes)
    with self.assertRaisesRegex(RuntimeError, "communication failed"):
      await self.driver.get_stored_exposure_times()
    self.assertEqual(self.usb.writes, writes)
