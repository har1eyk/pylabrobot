"""Check bounded USB aggregation with fake packets and real executor threads."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pylabrobot.io.usb import USB


class _Clock:
  """Advance only the transport clock, independently of asyncio's event-loop clock."""

  def __init__(self) -> None:
    """Retain separate elapsed and adjustable wall time."""
    self.elapsed = 0.0
    self.wall_offset = 0.0

  def monotonic(self) -> float:
    """Return monotonic elapsed seconds."""
    return self.elapsed

  def time(self) -> float:
    """Return a wall clock that can jump during a read."""
    return self.elapsed + self.wall_offset


class USBReadTests(unittest.IsolatedAsyncioTestCase):
  """Bound successive full packets without dropping bytes received before timeout."""

  async def test_last_submillisecond_budget_does_not_disable_libusb_timeout(self) -> None:
    """A positive fractional budget must not become libusb's unlimited zero timeout."""
    io = USB(1, 2, "fake USB")
    io.dev = Mock()
    io.read_endpoint = SimpleNamespace(wMaxPacketSize=64)  # type: ignore[assignment]
    clock = _Clock()

    def packet(size: object = None, timeout: float = 3, endpoint: object = None) -> bytearray:
      """Check the effective millisecond timeout without calling a native device."""
      assert int(timeout * 1000) == 1
      clock.elapsed += 0.001
      return bytearray(b"x")

    with (
      ThreadPoolExecutor(max_workers=1) as executor,
      patch.object(io, "_read_packet", side_effect=packet),
      patch("pylabrobot.io.usb.time", clock),
    ):
      io._read_executor = executor
      result = await io.read(timeout=0.0005, size=16384)  # type: ignore[arg-type]
    io._read_executor = None
    assert result == b"x"

  async def test_continuous_full_packets_share_one_deadline(self) -> None:
    """A stream of endpoint-sized packets must not extend an aggregate read indefinitely."""
    for requested in (None, 16384):
      with self.subTest(size=requested):
        io = USB(1, 2, "fake USB", packet_read_timeout=3)
        io.dev = Mock()
        io.read_endpoint = SimpleNamespace(wMaxPacketSize=64)  # type: ignore[assignment]
        clock = _Clock()
        timeouts: list[float] = []

        def packet(size: object = None, timeout: float = 3, endpoint: object = None) -> bytearray:
          """Supply eight packets over one second, with a guard against unbounded old code."""
          timeouts.append(timeout)
          if len(timeouts) > 16:
            raise AssertionError("Aggregate read exceeded its deadline")
          clock.elapsed += 0.125
          return bytearray(b"x" * 64)

        with (
          ThreadPoolExecutor(max_workers=1) as executor,
          patch.object(io, "_read_packet", side_effect=packet),
          patch("pylabrobot.io.usb.time", clock),
        ):
          io._read_executor = executor
          result = await io.read(timeout=1, size=requested)
        io._read_executor = None
        assert result == b"x" * 512
        assert clock.elapsed == 1
        assert timeouts == [1 - i * 0.125 for i in range(8)]

  async def test_empty_read_timeout_ignores_wall_clock_adjustments(self) -> None:
    """Clock synchronization must neither prolong nor prematurely terminate a USB deadline."""
    for jump in (-100, 100):
      with self.subTest(wall_clock_jump=jump):
        io = USB(1, 2, "fake USB")
        io.dev = Mock()
        io.read_endpoint = SimpleNamespace(wMaxPacketSize=64)  # type: ignore[assignment]
        clock = _Clock()
        timeouts: list[float] = []

        def packet(size: object = None, timeout: float = 3, endpoint: object = None) -> None:
          """Return no data while simulating a bounded packet timeout and a clock jump."""
          timeouts.append(timeout)
          if len(timeouts) > 16:
            raise AssertionError("Wall-clock adjustment extended USB timeout")
          clock.elapsed += 0.125
          clock.wall_offset = jump

        with (
          ThreadPoolExecutor(max_workers=1) as executor,
          patch.object(io, "_read_packet", side_effect=packet),
          patch("pylabrobot.io.usb.time", clock),
        ):
          io._read_executor = executor
          with self.assertRaises(TimeoutError):
            await io.read(timeout=1, size=16384)
        io._read_executor = None
        assert clock.elapsed == 1
        assert timeouts == [1 - i * 0.125 for i in range(8)]
