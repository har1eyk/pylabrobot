"""Check USB transfer termination without a connected device."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from pylabrobot.io.usb import USB


class USBWriteTests(unittest.IsolatedAsyncioTestCase):
  """Explicit-length protocols must be able to suppress automatic zero-length writes."""

  async def test_default_termination_and_explicit_length_protocol(self) -> None:
    """Preserve default behavior while matching the 7500's exact 64/61-byte boundaries."""
    for send_zlp, size, expected_lengths in (
      (True, 64, [64, 0]),
      (False, 64, [64]),
      (True, 61, [61]),
      (False, 61, [61]),
      (False, 128, [128]),
      (True, 128, [128, 0]),
    ):
      with self.subTest(send_zlp=send_zlp, size=size):
        io = USB(1, 2, "test USB", send_zero_length_packet=send_zlp)
        device = Mock()
        endpoint = SimpleNamespace(wMaxPacketSize=64)
        io.dev = device
        io.read_endpoint = endpoint  # type: ignore[assignment]
        io.write_endpoint = endpoint  # type: ignore[assignment]
        with ThreadPoolExecutor(max_workers=1) as executor:
          io._write_executor = executor
          await io.write(bytes(size), timeout=2)
        calls = device.write.call_args_list
        self.assertEqual([len(call.args[1]) for call in calls], expected_lengths)
        self.assertTrue(all(call.kwargs["timeout"] == 2000 for call in calls))

  def test_nondefault_termination_survives_serialization(self) -> None:
    """A reconstructed transport must retain its wire-termination setting."""
    io = USB(1, 2, "test USB", send_zero_length_packet=False)
    self.assertIs(io.serialize()["send_zero_length_packet"], False)
    default = USB(1, 2, "test USB")
    self.assertTrue(default.send_zero_length_packet)
    self.assertNotIn("send_zero_length_packet", default.serialize())
