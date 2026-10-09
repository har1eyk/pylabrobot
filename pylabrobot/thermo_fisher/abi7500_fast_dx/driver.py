"""USB connection and observation of the ABI 7500 Fast Dx reference instrument.

Run execution and optical acquisition are not exposed until their sequencing and recovery are
validated. This driver never loads a program, starts heating, moves filters, or changes lamp power.
"""

import asyncio
import logging
import math
from typing import Awaitable, Optional, TypeVar

from pylabrobot.io.usb import USB

from .camera import CameraGeometry
from .protocol import (
  ABI7500ProtocolError,
  ThermalPacket,
  ThermalPacketDecoder,
  decode_serial_read,
  encode_serial_read,
  encode_serial_write,
)
from .status import ABI7500Identity, ABI7500Status

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class ABI7500DeviceError(RuntimeError):
  """A matched thermal reply contains a nonzero firmware error field.

  The original reply is retained. Firmware errors are distinct from vendor application result
  codes such as -38 (user cancellation); no speculative error-name mapping is applied.
  """

  def __init__(self, reply: ThermalPacket) -> None:
    """Retain the device's command, sequence, error code and payload."""
    self.reply = reply
    super().__init__(
      f"ABI 7500 command 0x{reply.command:04x}, sequence {reply.sequence}: "
      f"firmware error 0x{reply.error_code:04x}"
    )


async def _finish_io(operation: Awaitable[_T]) -> _T:
  """Let a bounded USB operation finish before propagating cancellation.

  USB uses executor threads. Cancelling its Python await does not stop an in-flight libusb call;
  waiting prevents releasing the transaction lock or closing the device underneath that call.
  """
  task = asyncio.ensure_future(operation)
  cancellation: Optional[asyncio.CancelledError] = None
  while True:
    try:
      result = await asyncio.shield(task)
    except asyncio.CancelledError as exc:
      if task.cancelled():
        raise
      cancellation = exc
    except Exception:
      if cancellation is not None:
        logger.debug("USB operation failed during cancellation", exc_info=True)
        raise cancellation
      raise
    else:
      if cancellation is not None:
        raise cancellation
      return result


class ABI7500FastDx:
  """Experimental USB connection, identity and status queries for the 7500 Fast Dx.

  This is not yet a thermal-run API. Setup initializes the camera/serial bridge using the captured
  reference handshake and rejects thermal bootloader mode. No vendor DLL or Windows process is
  required. All communication uses :class:`pylabrobot.io.usb.USB`.

  Args:
    device_address: USB bus address to select when multiple matching cameras are connected.
    serial_number: USB descriptor serial number, not the calibration or thermal serial number.
    command_timeout: Maximum seconds per complete command exchange, including fragmented replies.
  """

  def __init__(
    self,
    device_address: Optional[int] = None,
    serial_number: Optional[str] = None,
    command_timeout: float = 10.0,
  ) -> None:
    """Create the transport without opening hardware."""
    if not math.isfinite(command_timeout) or command_timeout < 0.001:
      raise ValueError("command_timeout must be finite and at least 0.001 seconds")
    self.io = USB(
      id_vendor=0x0C2B,
      id_product=0x0300,
      human_readable_device_name="ABI 7500 Fast Dx",
      device_address=device_address,
      serial_number=serial_number,
      read_endpoint_address=0x85,
      write_endpoint_address=0x06,
      packet_read_timeout=min(0.25, command_timeout),
      read_timeout=max(1, math.ceil(command_timeout)),
      write_timeout=max(1, math.ceil(command_timeout)),
      send_zero_length_packet=False,
    )
    self.command_timeout = command_timeout
    self._lock = asyncio.Lock()
    self._connected = False
    self._fault: Optional[BaseException] = None
    self._sequence = 0
    self._heartbeat_task: Optional[asyncio.Task[None]] = None
    self._geometry: Optional[CameraGeometry] = None

  @property
  def is_connected(self) -> bool:
    """Whether setup succeeded and no communication failure has been detected."""
    return self._connected and self._fault is None

  @property
  def camera_geometry(self) -> CameraGeometry:
    """Return the geometry queried during the current connection."""
    if self._geometry is None:
      raise RuntimeError("Camera geometry is unavailable; call setup() first")
    return self._geometry

  def _check_connected(self) -> None:
    """Prevent traffic after a failed or incomplete transaction."""
    if self._fault is not None:
      raise RuntimeError(
        "ABI 7500 communication failed; call stop() before setup()"
      ) from self._fault
    if not self._connected:
      raise RuntimeError("ABI 7500 is not connected; call setup() first")

  def _deadline(self) -> float:
    """Start a deadline shared by all fragments of one command."""
    return asyncio.get_running_loop().time() + self.command_timeout

  def _remaining(self, deadline: float) -> float:
    """Return positive time remaining without restarting the command timeout."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
      raise TimeoutError("ABI 7500 command timed out")
    return remaining

  async def _write(self, data: bytes, deadline: float) -> None:
    """Write one captured transfer boundary within the transaction deadline."""
    # libusb interprets zero milliseconds as an infinite timeout.
    await _finish_io(self.io.write(data, timeout=max(0.001, self._remaining(deadline))))

  async def _read_exactly(self, size: int, deadline: float) -> bytes:
    """Assemble a fixed-size USB response without dropping or accepting extra bytes."""
    data = bytearray()
    while len(data) < size:
      chunk = await _finish_io(
        self.io.read(size=size - len(data), timeout=max(1, math.ceil(self._remaining(deadline))))
      )
      if not chunk or len(chunk) > size - len(data):
        raise ABI7500ProtocolError("Empty or oversized USB response fragment")
      data.extend(chunk)
    self._remaining(deadline)
    return bytes(data)

  async def _camera_query(self, command: bytes, size: int) -> bytes:
    """Perform a fixed-length bridge query; the caller owns the transaction lock."""
    deadline = self._deadline()
    await self._write(command, deadline)
    return await self._read_exactly(size, deadline)

  async def _thermal_query(self, command: int, response_size: int) -> bytes:
    """Exchange one query, matching command/address/sequence without automatically retrying.

    Each RR transfer includes a valid-byte count and arbitrary padding. Empty serial reads are
    polled within the original deadline. Any incomplete transaction makes subsequent traffic
    unsafe until the connection is closed and initialized again.
    """
    self._sequence = (self._sequence + 1) & 0xFFFFFFFF
    request = ThermalPacket(command, self._sequence)
    deadline = self._deadline()
    for chunk in encode_serial_write(request.encode()):
      await self._write(chunk, deadline)
    decoder = ThermalPacketDecoder()
    received = 0
    expected_size = response_size + 21
    while received < expected_size:
      capacity = expected_size - received
      await self._write(encode_serial_read(capacity), deadline)
      wire = await self._read_exactly(capacity + 2, deadline)
      data = decode_serial_read(wire, requested_size=capacity)
      received += len(data)
      replies = decoder.feed(data)
      if replies:
        decoder.finish()
        if len(replies) != 1:
          raise ABI7500ProtocolError("Multiple thermal replies to a single query")
        reply = replies[0]
        if (reply.command, reply.sequence, reply.address) != (
          request.command,
          request.sequence,
          request.address,
        ):
          raise ABI7500ProtocolError("Thermal response does not match the pending query")
        if reply.error_code:
          raise ABI7500DeviceError(reply)
        if len(reply.payload) != response_size:
          raise ABI7500ProtocolError("Unexpected thermal response payload size")
        return reply.payload
      if not data:
        await asyncio.sleep(min(0.01, self._remaining(deadline)))
    raise ABI7500ProtocolError("Incomplete thermal reply at the expected response length")

  async def _heartbeat(self) -> None:
    """Send the captured camera idle keepalive, serialized with every other exchange."""
    try:
      while True:
        await asyncio.sleep(0.5)
        async with self._lock:
          if not self.is_connected:
            return
          await self._write(b"C:", self._deadline())
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      self._fault = exc
      logger.error("ABI 7500 camera keepalive failed; connection requires reopening", exc_info=True)

  async def setup(self) -> None:
    """Open USB and initialize the captured bridge handshake, without thermal/optical actuation.

    Repeated setup on a healthy connection is a no-op. Setup failures close the USB handle.
    A failed established connection requires an explicit stop before reconnection.
    """
    async with self._lock:
      if self._fault is not None:
        self._check_connected()
      if self._connected:
        return
      logger.warning(
        "ABI 7500 Fast Dx integration is incomplete. Connection and status were checked on one "
        "G2.10 instrument; thermal-run control and live optical acquisition remain unimplemented "
        "and unverified."
      )
      try:
        await _finish_io(self.io.setup(empty_buffer=False))
        if await self._camera_query(b"CH   5", 5) != b"ready":
          raise ABI7500ProtocolError("Camera is not ready for a connection")
        # Opaque initialization metadata from the reference handshake; not a firmware upload.
        await self._write(b"Cf  15,,.,1.05,2.19,,", self._deadline())
        camera = await self._camera_query(b"CF  64", 64)
        self._geometry = CameraGeometry.decode(await self._camera_query(b"CI   4", 4))
        await self._write(b"RP  109600,8,0,0", self._deadline())
        await self._write(b"RC", self._deadline())
        await self._write(b"RP  109600,8,0,0", self._deadline())
        self._sequence = 0
        boot_state = await self._thermal_query(0x2F, 10)
        if boot_state.split(b"\x00", 1)[0] != b"APPL":
          raise ABI7500ProtocolError("Thermal controller is not in application mode")
        identity = ABI7500Identity(camera, await self._thermal_query(0x0A, 70))
        if not identity.camera_description.startswith("Hamamatsu,C9254-"):
          raise ABI7500ProtocolError("Unexpected camera identity")
        self._connected = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        logger.info(
          "Connected to %s, thermal firmware %s",
          identity.camera_description,
          identity.thermal_version,
        )
      except BaseException:
        self._geometry = None
        self._connected = False
        await _finish_io(self.io.stop())
        raise

  async def get_identity(self) -> ABI7500Identity:
    """Query the camera description and thermal firmware version."""
    async with self._lock:
      self._check_connected()
      try:
        camera = await self._camera_query(b"CF  64", 64)
        return ABI7500Identity(camera, await self._thermal_query(0x0A, 70))
      except BaseException as exc:
        self._fault = exc
        raise

  async def get_status(self) -> ABI7500Status:
    """Query temperatures and native sequence progress without changing the thermal program."""
    async with self._lock:
      self._check_connected()
      try:
        return ABI7500Status(await self._thermal_query(0x29, 50))
      except BaseException as exc:
        self._fault = exc
        raise

  async def stop(self) -> None:
    """Close the USB connection and stop keepalives; this does not cancel a thermal run.

    Safe to call repeatedly. A future explicit cancellation API must be used for run cancellation;
    disconnecting a control computer is not evidence that an instrument has stopped heating.
    """
    async with self._lock:
      self._connected = False
      if self._heartbeat_task is not None:
        self._heartbeat_task.cancel()
        try:
          await self._heartbeat_task
        except asyncio.CancelledError:
          pass
        self._heartbeat_task = None
      try:
        await _finish_io(self.io.stop())
      finally:
        self._geometry = None
        self._fault = None
      logger.info("ABI 7500 USB connection closed")
