"""USB thermal/optical control and scheduled camera collection for the ABI 7500 Fast Dx."""

import asyncio
import logging
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Optional, TypeVar

from pylabrobot.io.usb import USB

from ._run_journal import RunJournal
from .acquisition import CameraAcquisition, CameraExposure, OpticalState, _validate_acquisition_id
from .camera import CameraGeometry
from .collection import CollectionPlan, CollectionPoint, CollectionRecord, FluorescenceCollection
from .protocol import (
  ABI7500ProtocolError,
  ThermalPacket,
  ThermalPacketDecoder,
  decode_serial_read,
  encode_serial_read,
  encode_serial_write,
)
from .run import (
  ABI7500History,
  ABI7500RunError,
  ThermalRun,
  _validate_run_id,
  _validate_run_program,
)
from .status import ABI7500Identity, ABI7500Status
from .thermal_program import ThermalProgram

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
  """USB observation and experimental thermal/optical runs for the 7500 Fast Dx.

  Setup initializes the captured camera/serial bridge handshake and rejects thermal bootloader
  mode. A 35 °C/60-second hold, start/cancel and a normally completed two-cycle 35 °C/30 °C run
  with 30 hold-associated images were checked on G2.10. Broader thermal programs and
  communication-fault recovery remain unverified.
  Run control requires a journal. No vendor DLL or Windows process is required; all communication
  uses :class:`pylabrobot.io.usb.USB`. Offline processing reproduces one recorded vendor
  acquisition. Current calibration validity is not established; saturation flags are preserved.

  Args:
    device_address: USB bus address to select when multiple matching cameras are connected.
    serial_number: USB descriptor serial number, not the calibration or thermal serial number.
    command_timeout: Maximum seconds per complete command exchange, including fragmented replies.
    run_journal: Persistent local SQLite path, reused for this instrument across processes/runs.
      Required for runs, snapshots and optical control. Keep it outside Git and outside
      synchronized/network folders.
  """

  def __init__(
    self,
    device_address: Optional[int] = None,
    serial_number: Optional[str] = None,
    command_timeout: float = 10.0,
    run_journal: Optional[Path] = None,
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
    self._run_lock = asyncio.Lock()
    self._journal = None if run_journal is None else RunJournal(run_journal)
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

  def _remaining(self, deadline: float, *, context: str = "command") -> float:
    """Return positive time remaining, identifying the expired operation without resetting it."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
      raise TimeoutError(f"ABI 7500 {context} timed out")
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

  async def _thermal_query(self, command: int, response_size: int, payload: bytes = b"") -> bytes:
    """Exchange one command, matching command/address/sequence without automatically retrying.

    Each RR transfer includes a valid-byte count and arbitrary padding. Empty serial reads are
    polled within the original deadline. Any incomplete transaction makes subsequent traffic
    unsafe until the connection is closed and initialized again.
    """
    self._sequence = (self._sequence + 1) & 0xFFFFFFFF
    request = ThermalPacket(command, self._sequence, payload)
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

  async def _exchange(self, command: int, response_size: int, payload: bytes = b"") -> bytes:
    """Serialize an established-connection command and fault the connection on any interruption."""
    async with self._lock:
      self._check_connected()
      try:
        return await self._thermal_query(command, response_size, payload)
      except BaseException as exc:
        self._fault = exc
        raise

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
        "ABI 7500 Fast Dx integration is experimental. On G2.10, a 35 C/60-second hold, "
        "start/cancel and a normally completed two-cycle 35 C/30 C run with 30 hold-associated "
        "images were checked. Broader thermal programs and communication-fault recovery are "
        "NOT validated. "
        "Offline camera processing matches one recorded vendor acquisition; current calibration "
        "validity is NOT established."
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

  async def get_history(self) -> ABI7500History:
    """Read the last thermal history header; this need not describe the active sequence."""
    return ABI7500History(await self._exchange(0x19, 94))

  def _require_journal(self) -> RunJournal:
    """Require durable identity tracking before exposing any run mutation."""
    if self._journal is None:
      raise ABI7500RunError(
        "Run control, snapshots and optical control require a persistent run_journal path"
      )
    return self._journal

  async def _query_optics(self) -> OpticalState:
    """Read native optical observations while the caller owns the USB transaction lock."""
    motor = await self._thermal_query(0x6C, 2)
    position = await self._thermal_query(0x6B, 2)
    lamp = await self._thermal_query(0x66, 4)
    return OpticalState(position, motor, lamp)

  async def get_optical_state(self) -> OpticalState:
    """Read filter position, motor state and lamp words without changing them."""
    async with self._lock:
      self._check_connected()
      try:
        return await self._query_optics()
      except BaseException as exc:
        self._fault = exc
        raise

  async def _optical_preflight(self) -> tuple[ABI7500Status, OpticalState]:
    """Check identity, journal ownership and inactivity under both operation locks."""
    journal = self._require_journal()
    self._check_connected()
    try:
      identity = ABI7500Identity(
        await self._camera_query(b"CF  64", 64), await self._thermal_query(0x0A, 70)
      )
      before = ABI7500Status(await self._thermal_query(0x29, 50))
      optics = await self._query_optics()
      after = ABI7500Status(await self._thermal_query(0x29, 50))
      ready = await self._camera_query(b"CH   5", 5)
    except BaseException as exc:
      self._fault = exc
      raise
    if (
      identity.camera_description.split(",")[:3] != ["Hamamatsu", "C9254-03", "2.20"]
      or identity.thermal_version != "G2.10"
    ):
      raise ABI7500RunError("Optical control requires the reference C9254-03/2.20 and G2.10")
    journal.check_optical_control(identity.camera_description, identity.thermal_version)
    if (
      before.phase != "inactive"
      or after.phase != "inactive"
      or before.state_code not in (1, 2)
      or after.state_code not in (1, 2)
      or before.run_start_record != after.run_start_record
      or ready != b"ready"
    ):
      raise ABI7500RunError("Optical control requires an idle camera and inactive thermal run")
    if (
      optics.filter_position not in range(6)
      or optics.motor_response not in (b"\x00\x00", b"\x00\x01")
      or optics.lamp_power not in (0, 1, 2)
    ):
      raise ABI7500ProtocolError("Unrecognized optical state")
    optics.lamp_enabled  # Validate the enable word before allowing any mutation.
    return after, optics

  async def _wait_filter(self, deadline: float) -> None:
    """Wait for the observed stationary state without reissuing a move."""
    while True:
      self._remaining(deadline)
      state = await self._thermal_query(0x6C, 2)
      self._remaining(deadline)
      if state == b"\x00\x00":
        return
      if state != b"\x00\x01":
        raise ABI7500ProtocolError("Unknown filter motor state")
      await asyncio.sleep(min(0.1, self._remaining(deadline)))

  async def _finish_optical_control(self, before: ABI7500Status) -> OpticalState:
    """Confirm inactivity and retain the complete optical readback after a change."""
    optics = await self._query_optics()
    after = ABI7500Status(await self._thermal_query(0x29, 50))
    if (
      after.phase != "inactive"
      or after.state_code not in (1, 2)
      or after.run_start_record != before.run_start_record
      or not optics.is_stationary
    ):
      raise ABI7500RunError("Instrument state changed during optical control")
    return optics

  async def set_filter_position(self, position: int, *, timeout: float = 15.0) -> OpticalState:
    """Move to an absolute native filter position 0–5 and verify stationary readback.

    Positions are firmware indices, not dye assignments. The reference vendor acquisition uses
    positions 1–5 for saved filters 5–1 respectively; position 0 also occurs at idle. No homing or
    calibration position-setting command is sent. A moving filter is awaited before reading its
    position. Matching positions cause no move. The timeout covers waiting for motion; each USB
    exchange is also bounded by command_timeout. Positions 0, 1 and 5 were checked on hardware.

    Requires a persistent run journal, an inactive instrument and per-session approval. A failure
    faults the connection; after stop()/setup(), repeating the desired position checks the actual
    state before deciding whether a move is necessary. No mutation is automatically retried.
    """
    if type(position) is not int or position not in range(6):
      raise ValueError("position must be an integer from 0 through 5")
    if not math.isfinite(timeout) or timeout <= 0:
      raise ValueError("timeout must be finite and positive")
    async with self._run_lock, self._lock:
      before, optics = await self._optical_preflight()
      try:
        deadline = asyncio.get_running_loop().time() + timeout
        if not optics.is_stationary:
          await self._wait_filter(deadline)
          settled = await self._finish_optical_control(before)
          if (
            settled.filter_position not in range(6) or settled.lamp_response != optics.lamp_response
          ):
            raise ABI7500ProtocolError("Unexpected optical state after existing filter motion")
          optics = settled
        if optics.filter_position != position:
          await self._thermal_query(0x69, 0, position.to_bytes(2, "big"))
          await self._wait_filter(deadline)
        result = await self._finish_optical_control(before)
        if result.filter_position != position or result.lamp_response != optics.lamp_response:
          raise ABI7500ProtocolError("Filter position or lamp readback changed unexpectedly")
        return result
      except BaseException as exc:
        self._fault = exc
        raise

  async def set_lamp(self, enabled: bool, *, power: Optional[int] = None) -> OpticalState:
    """Set lamp enable and its native power code, checking readback and skipping satisfied writes.

    Enabling requires explicit power 0 or 1. These are vendor codes, not calibrated intensity.
    Disabling requires power=None and only writes the enable command. The recovered vendor order
    is enable (0x64), 250 ms delay, then power (0x65), and 250 ms before readback.
    No lamp-usage reset or automatic exposure is performed.
    This does not wait for optical stabilization; readback is not a brightness measurement.

    Requires the reference firmware, an inactive instrument, stationary filter, persistent journal
    and per-session approval. Lamp readback alone does not establish illumination. Failed exchanges
    are never retried automatically. stop() closes USB and does not switch off the lamp; after an
    ambiguous failure reconnect and read its state before further actions.
    """
    if type(enabled) is not bool:
      raise ValueError("enabled must be bool")
    if enabled and (type(power) is not int or power not in (0, 1)):
      raise ValueError("Enabling the lamp requires native power 0 or 1")
    if not enabled and power is not None:
      raise ValueError("Disabling the lamp requires power=None")
    async with self._run_lock, self._lock:
      before, optics = await self._optical_preflight()
      if not optics.is_stationary:
        raise ABI7500RunError("Lamp control requires a stationary filter")
      try:
        changed_enable = optics.lamp_enabled != enabled
        changed_power = enabled and (changed_enable or optics.lamp_power != power)
        if changed_enable:
          await self._thermal_query(0x64, 0, int(enabled).to_bytes(2, "big"))
          if enabled:
            await asyncio.sleep(0.25)
        if changed_power:
          assert power is not None
          await self._thermal_query(0x65, 0, power.to_bytes(2, "big"))
        if changed_enable or changed_power:
          await asyncio.sleep(0.25)
        result = await self._finish_optical_control(before)
        if (
          result.lamp_enabled != enabled
          or (enabled and result.lamp_power != power)
          or result.filter_position != optics.filter_position
        ):
          raise ABI7500ProtocolError("Lamp or filter readback does not match the requested state")
        return result
      except BaseException as exc:
        self._fault = exc
        raise

  async def get_acquisition(self, acquisition_id: str) -> CameraAcquisition:
    """Read saved complete or uncertain snapshot data, without any instrument I/O.

    An incomplete transfer cannot be resumed: a second CC could overwrite its source image.
    This method also works after stop() or in a fresh controller using the same journal.
    """
    _validate_acquisition_id(acquisition_id)
    async with self._run_lock:
      result = self._require_journal().get_acquisition(acquisition_id)
      if result is None:
        raise ABI7500RunError(f"Unknown acquisition {acquisition_id!r}")
      return result

  async def _capture_reserved(
    self, result: CameraAcquisition, collection: Optional[CollectionRecord] = None
  ) -> CameraAcquisition:
    """Execute one reserved exposure under both locks, retaining partial data on interruption."""
    journal = self._require_journal()
    data = bytearray()
    readiness: list[bytes] = []
    try:
      reply = await self._camera_query(result.exposure.encode(), 2)
      result = journal.save_acquisition(replace(result, exposure_reply=reply))
      result.exposure.validate_reply(reply)
      if collection is not None:
        status = ABI7500Status(await self._thermal_query(0x29, 50))
        if (
          not collection.point.matches(status)
          or status.run_start_record != collection.before.run_start_record
          or status.remaining_hold_time is None
          or status.remaining_hold_time < math.ceil(result.exposure.duration) + 2
        ):
          raise ABI7500RunError(
            "Collection hold ended or insufficient time remains before exposure"
          )
        result = journal.save_acquisition(replace(result, thermal_before=status))
      await self._write(b"CC", self._deadline())
      loop = asyncio.get_running_loop()
      deadline = loop.time() + result.exposure.duration + self.command_timeout
      while True:
        await asyncio.sleep(min(0.01, self._remaining(deadline)))
        await self._write(b"CH   5", deadline)
        state = await self._read_exactly(5, deadline)
        readiness.append(state)
        if state == b"ready":
          break
        if state != b"not r":
          raise ABI7500ProtocolError(f"Unexpected camera readiness reply: {state!r}")
      deadline = self._deadline()
      await self._write(result.geometry.encode_read(), deadline)
      while len(data) < result.geometry.transfer_size:
        size = min(4096, result.geometry.transfer_size - len(data))
        chunk = await _finish_io(
          self.io.read(size=size, timeout=max(1, math.ceil(self._remaining(deadline))))
        )
        if not chunk or len(chunk) > size:
          raise ABI7500ProtocolError("Empty or oversized camera image fragment")
        data.extend(chunk)
        self._remaining(deadline)
      result = journal.save_acquisition(
        replace(result, wire_data=bytes(data), readiness_replies=tuple(readiness))
      )
      # Persist the frame before requesting final observations, which can themselves fail.
      result = replace(result, optics_after=await self._query_optics())
      status = ABI7500Status(await self._thermal_query(0x29, 50))
      result = replace(result, thermal_after=status)
      same_phase = status is not None and (
        (status.phase == "inactive" and status.state_code in (1, 2))
        if collection is None
        else collection.point.matches(status)
      )
      if (
        not same_phase
        or status is None
        or status.run_start_record != result.thermal_before.run_start_record
        or result.optics_after != result.optics_before
      ):
        raise ABI7500RunError("Instrument state changed during the raw snapshot")
      result = journal.save_acquisition(
        replace(result, state="complete", finished_at=datetime.now(timezone.utc).isoformat())
      )
      logger.info("Saved raw camera acquisition %s (%d bytes)", result.acquisition_id, len(data))
      return result
    except BaseException as exc:
      self._fault = exc
      journal.save_acquisition(
        replace(
          result,
          state="uncertain",
          wire_data=bytes(data),
          readiness_replies=tuple(readiness),
          error=f"{type(exc).__name__}: {exc}",
        )
      )
      logger.error(
        "Camera acquisition %s interrupted; CC will not be repeated", result.acquisition_id
      )
      raise

  async def capture_frame(
    self, acquisition_id: str, *, exposure: float = 0.256
  ) -> CameraAcquisition:
    """Acquire one experimental raw idle snapshot, preserving the existing lamp/filter state.

    This changes camera exposure and sends CC once. It does not move the plate/filter, switch the
    lamp, heat, or change an existing thermal setpoint. Supports only C9254-03/2.20, G2.10 and
    321×245 wire geometry. Exposure is in seconds, limited to 0.005–1.048. The thermal controller
    must be inactive and every journaled thermal run resolved. Requires local hardware approval.

    The same ID/exposure returns its journal record without I/O, including after an ambiguous
    failure. Reuse the same local journal across processes. An interrupted snapshot faults the
    connection: stop()/setup() is required before further I/O. Python cancellation does not send
    an exposure-abort command. A new ID intentionally requests a separate exposure; never change
    IDs merely to retry. Results contain raw pixels and observations, not per-well fluorescence.
    """
    _validate_acquisition_id(acquisition_id)
    requested = CameraExposure(exposure)
    async with self._run_lock:
      journal = self._require_journal()
      prior = journal.get_acquisition(acquisition_id)
      if prior is not None:
        if prior.exposure != requested:
          raise ABI7500RunError("An acquisition ID cannot be reused with a different exposure")
        return prior
      async with self._lock:
        self._check_connected()
        try:
          camera = await self._camera_query(b"CF  64", 64)
          identity = ABI7500Identity(camera, await self._thermal_query(0x0A, 70))
          geometry = CameraGeometry.decode(await self._camera_query(b"CI   4", 4))
          status = ABI7500Status(await self._thermal_query(0x29, 50))
          optics = await self._query_optics()
          after = ABI7500Status(await self._thermal_query(0x29, 50))
          ready = await self._camera_query(b"CH   5", 5)
        except BaseException as exc:
          self._fault = exc
          raise
        if (
          identity.camera_description.split(",")[:3] != ["Hamamatsu", "C9254-03", "2.20"]
          or identity.thermal_version != "G2.10"
          or geometry != CameraGeometry(321, 245)
        ):
          raise ABI7500RunError("Snapshot requires the reference C9254-03/2.20, G2.10 and geometry")
        if (
          status.phase != "inactive"
          or after.phase != "inactive"
          or status.state_code not in (1, 2)
          or after.state_code not in (1, 2)
          or status.run_start_record != after.run_start_record
          or not optics.is_stationary
          or optics.filter_position not in range(6)
          or ready != b"ready"
        ):
          raise ABI7500RunError(
            "Snapshot requires an idle camera, inactive run and stationary filter"
          )
        result = CameraAcquisition(
          acquisition_id,
          requested,
          identity.camera_description,
          identity.thermal_version,
          geometry,
          datetime.now(timezone.utc).isoformat(),
          after,
          optics,
        )
        journal.reserve_acquisition(result)
        logger.warning("Starting experimental raw camera snapshot %s", acquisition_id)
        return await self._capture_reserved(result)

  async def _check_run_identity(self, run: ThermalRun) -> None:
    """Refuse to reconcile or cancel a checkpoint on a different controller."""
    identity = await self.get_identity()
    if (identity.camera_description, identity.thermal_version) != (
      run.camera_description,
      run.thermal_version,
    ):
      raise ABI7500RunError("Connected instrument does not match the run checkpoint")

  async def _observe_run(self, run: ThermalRun) -> ThermalRun:
    """Reconcile independent status/history evidence without ever resending Start."""
    journal = self._require_journal()
    before = await self.get_status()
    history = await self.get_history()
    status = await self.get_status()
    observed = replace(run, last_status=status, history=history, state="uncertain")
    active_phases = ("ramping", "holding", "paused", "modified")
    active = status.phase in active_phases
    idle = status.phase == "inactive" and status.state_code in (1, 2)
    if (
      before.run_start_record != status.run_start_record
      or (before.phase in active_phases) != active
    ):
      return journal.save(observed)
    tagged = history.method_name == run.run_id and history.user_name == "PLR"
    token = run.start_record
    if tagged and history.run_start_record != run.baseline_start_record:
      if token is not None and token != history.run_start_record:
        return journal.save(observed)
      token = history.run_start_record
    if (
      token is None
      and run.start_acknowledged
      and active
      and status.run_start_record != run.baseline_start_record
    ):
      token = status.run_start_record
    if token is not None and token != bytes(9):
      observed = replace(observed, start_record=token)
      if status.run_start_record != token:
        return journal.save(observed)
      if active:
        observed = replace(observed, state="cancelling" if run.cancel_requested else "running")
      elif idle and tagged and history.run_start_record == token:
        if history.outcome == "completed":
          observed = replace(observed, state="completed")
        elif history.outcome == "aborted":
          if not run.cancel_requested:
            observed = replace(observed, state="aborted")
          elif run.idle_temperature_set:
            observed = replace(observed, state="cancelled")
          else:
            observed = replace(observed, state="cancelling")
    elif run.state == "preparing" and idle and status.run_start_record == run.baseline_start_record:
      # The durable starting checkpoint always precedes transmission of 0x13.
      observed = replace(observed, state="not_started")
    elif (
      run.state == "starting"
      and run.start_acknowledged
      and status.phase == "inactive"
      and status.state_code in (1, 2, 3)
      and status.run_start_record == run.baseline_start_record
    ):
      observed = replace(observed, state="starting")
    return journal.save(observed)

  async def get_run(self, run_id: str) -> ThermalRun:
    """Read a durable result or reconcile an unresolved run using fresh device evidence.

    After a communication failure, call stop()/setup() with the same journal before this method.
    Uncertain ownership stays uncertain; this method performs no thermal or plate actuation.
    """
    _validate_run_id(run_id)
    async with self._run_lock:
      run = self._require_journal().get(run_id)
      if run is None:
        raise ABI7500RunError(f"Unknown run {run_id!r}")
      if run.is_terminal:
        return run
      await self._check_run_identity(run)
      return await self._observe_run(run)

  async def _load_run_plate(self, timeout: float) -> None:
    """Load an unloaded plate once, then poll the captured block-state transition to 2."""
    state = int.from_bytes(await self._exchange(0x73, 2), "big")
    if state == 2:
      return
    if state != 0:
      raise ABI7500RunError(f"Plate is not in a known stationary state ({state})")
    await self._exchange(0x71, 0)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
      await asyncio.sleep(min(0.5, self._remaining(deadline)))
      state = int.from_bytes(await self._exchange(0x73, 2), "big")
      if state == 2:
        return
      if state not in (0, 1):
        raise ABI7500RunError(f"Unexpected plate-loading state ({state})")
      self._remaining(deadline)

  async def start_run(
    self,
    run_id: str,
    program: ThermalProgram,
    *,
    plate_timeout: float = 30.0,
    collection_plan: Optional[CollectionPlan] = None,
  ) -> ThermalRun:
    """Prepare and submit one experimental run, never automatically repeating Start.

    The supported configuration is the captured Standard mode, 20 µL, block 2, tube type 0 with
    finite cycle/hold records and firmware G2.10. The plate must be sealed and physically ready for
    the internal loading motion. This sends prepare/upload/load/start commands, which can cause
    movement and heating. It uses the instrument's cover behavior; custom cover settings are not
    provided. Requires approval appropriate to the local setup.

    An optional collection_plan records host-scheduled optical reads. Before starting, enable the
    lamp at power 1, allow it to settle, and set filter 0. Immediately await collect_fluorescence()
    after this call: start_run() alone does not acquire images or create a background collector.

    A successful acknowledgement returns ``starting``. Poll get_run() to observe actual progress.
    Repeating the same ID/program/plan returns its checkpoint without device writes, even after a
    timeout or process restart. An unresolved run blocks another ID. Do not delete the journal or
    switch journal paths to retry. Different IDs represent separate, intentional experiments.
    """
    _validate_run_id(run_id)
    _validate_run_program(program)
    if collection_plan is not None:
      collection_plan.points(program)
    if not math.isfinite(plate_timeout) or plate_timeout <= 0:
      raise ValueError("plate_timeout must be finite and positive")
    async with self._run_lock:
      journal = self._require_journal()
      prior = journal.get(run_id)
      if prior is not None:
        if prior.program != program or prior.collection_plan != collection_plan:
          raise ABI7500RunError(
            "A run ID cannot be reused with a different program or collection plan"
          )
        return prior
      identity = await self.get_identity()
      if identity.thermal_version != "G2.10":
        raise ABI7500RunError("Experimental run control supports only the reference G2.10 firmware")
      status = await self.get_status()
      history = await self.get_history()
      status_after = await self.get_status()
      if (
        status.phase != "inactive"
        or status_after.phase != "inactive"
        or status.state_code not in (1, 2)
        or status_after.state_code not in (1, 2)
        or status.run_start_record != status_after.run_start_record
      ):
        raise ABI7500RunError("Instrument is active, transitioning, or in an unrecognized state")
      if history.method_name == run_id:
        raise ABI7500RunError("Instrument history already contains this run ID; do not restart it")
      if collection_plan is not None:
        async with self._lock:
          _, optics = await self._optical_preflight()
          if (
            self._geometry != CameraGeometry(321, 245)
            or not optics.is_stationary
            or optics.filter_position != 0
            or not optics.lamp_enabled
            or optics.lamp_power != 1
          ):
            raise ABI7500RunError(
              "Optical run requires reference geometry, filter 0 and lamp on at power 1"
            )
      run = ThermalRun(
        run_id,
        program,
        identity.camera_description,
        identity.thermal_version,
        status.run_start_record,
        last_status=status_after,
        history=history,
        collection_plan=collection_plan,
      )
      journal.reserve(run)
      attempted_start = False
      try:
        logger.warning(
          "Starting experimental run %s (optical plan: %s)", run_id, collection_plan is not None
        )
        await self._exchange(0x1B, 0)
        await asyncio.sleep(1.0)
        await self._exchange(0x12, 0, run.wire_program.encode())
        await asyncio.sleep(0.5)
        await self._load_run_plate(plate_timeout)
        status = await self.get_status()
        if (
          status.phase != "inactive"
          or status.state_code not in (1, 2)
          or status.run_start_record != run.baseline_start_record
        ):
          raise ABI7500RunError("Instrument state changed before Start; refusing to start")
        run = journal.save(replace(run, state="starting", last_status=status))
        attempted_start = True
        await self._exchange(0x13, 0)
        run = journal.save(replace(run, start_acknowledged=True))
        logger.info(
          "Thermal run %s start acknowledged; progress requires status reconciliation", run_id
        )
        return run
      except BaseException:
        journal.save(replace(run, state="uncertain" if attempted_start else "not_started"))
        logger.error("Thermal run %s interrupted; Start will not be resent", run_id)
        raise

  def _read_collections(self, run: ThermalRun) -> tuple[FluorescenceCollection, ...]:
    """Hydrate existing collection records and partial frames without opening USB."""
    if run.collection_plan is None:
      raise ABI7500RunError("This thermal run has no optical collection plan")
    journal = self._require_journal()
    results = []
    for point in run.collection_plan.points(run.program):
      record = journal.get_collection(point.collection_id(run.run_id))
      if record is None:
        continue
      frames = []
      for acquisition_id in record.acquisition_ids:
        acquisition = journal.get_acquisition(acquisition_id)
        if acquisition is not None:
          frames.append(acquisition)
      results.append(FluorescenceCollection(record, tuple(frames)))
    return tuple(results)

  async def get_collections(self, run_id: str) -> tuple[FluorescenceCollection, ...]:
    """Read complete and uncertain optical collections offline, preserving missing/partial data.

    No record means that collection was never reserved, not that its thermal hold was skipped.
    Inspect get_run() and collection_error to distinguish thermal completion from optical success.
    """
    _validate_run_id(run_id)
    async with self._run_lock:
      run = self._require_journal().get(run_id)
      if run is None:
        raise ABI7500RunError(f"Unknown run {run_id!r}")
      return self._read_collections(run)

  async def _collect_point(self, run_id: str, point: CollectionPoint) -> None:
    """Reserve and execute one bounded read under run and USB transaction ownership."""
    async with self._run_lock:
      journal = self._require_journal()
      run = journal.get(run_id)
      if run is None or run.collection_plan is None or run.collection_error:
        raise ABI7500RunError("No eligible optical run")
      plan = run.collection_plan
      prior = journal.get_collection(point.collection_id(run_id))
      if prior is not None:
        if prior.state != "complete":
          raise ABI7500RunError("An uncertain collection cannot be repeated or resumed")
        return
      points = plan.points(run.program)
      if point not in points:
        raise ABI7500RunError("Point is not in this run's collection plan")
      for earlier in points[: points.index(point)]:
        saved = journal.get_collection(earlier.collection_id(run_id))
        if saved is None or saved.state != "complete":
          raise ABI7500RunError("An earlier collection is missing or uncertain")
      await self._check_run_identity(run)
      run = await self._observe_run(run)
      status = run.last_status
      if (
        run.state != "running"
        or status is None
        or not point.matches(status)
        or status.remaining_hold_time is None
        or not plan.read_window - 2 <= status.remaining_hold_time <= plan.read_window
      ):
        raise ABI7500RunError("Collection window was missed or run ownership changed")
      record = CollectionRecord(
        run_id,
        point,
        points.index(point) + 1,
        status,
        tuple(
          f"{point.collection_id(run_id)}-f{f.position}-e{index}"
          for f in plan.filters
          for index in range(3)
        ),
      )
      journal.reserve_collection(record)
      async with self._lock:
        self._check_connected()
        try:
          optics = await self._query_optics()
          if (
            not optics.is_stationary
            or not optics.lamp_enabled
            or optics.lamp_power != 1
            or optics.filter_position not in range(6)
            or await self._camera_query(b"CH   5", 5) != b"ready"
          ):
            raise ABI7500RunError(
              "Collection requires a ready camera and stationary illuminated optics"
            )
          for setting in plan.filters:
            status = ABI7500Status(await self._thermal_query(0x29, 50))
            if (
              not point.matches(status)
              or status.run_start_record != run.start_record
              or status.remaining_hold_time is None
              or status.remaining_hold_time < 4
            ):
              raise ABI7500RunError("Collection crossed a thermal boundary before filter selection")
            if optics.filter_position != setting.position:
              await self._thermal_query(0x69, 0, setting.position.to_bytes(2, "big"))
              await self._wait_filter(
                asyncio.get_running_loop().time() + min(15, status.remaining_hold_time - 2)
              )
            optics = await self._query_optics()
            if (
              not optics.is_stationary
              or optics.filter_position != setting.position
              or not optics.lamp_enabled
              or optics.lamp_power != 1
            ):
              raise ABI7500RunError("Optical readback disagrees with the collection plan")
            for index, exposure in enumerate(setting.exposures):
              acquisition = CameraAcquisition(
                f"{record.collection_id}-f{setting.position}-e{index}",
                CameraExposure(exposure),
                run.camera_description,
                run.thermal_version,
                CameraGeometry(321, 245),
                datetime.now(timezone.utc).isoformat(),
                status,
                optics,
              )
              journal.reserve_acquisition(acquisition, collection_id=record.collection_id)
              acquisition = await self._capture_reserved(acquisition, record)
              assert acquisition.thermal_after is not None
              status = acquisition.thermal_after
            logger.info(
              "Run %s collection %d captured physical filter %d",
              run_id,
              record.ordinal,
              setting.position,
            )
          after = ABI7500Status(await self._thermal_query(0x29, 50))
          if not point.matches(after) or after.run_start_record != run.start_record:
            raise ABI7500RunError("Collection crossed a thermal boundary")
          journal.save_collection(replace(record, state="complete", after=after))
        except BaseException as exc:
          self._fault = exc
          error = f"{type(exc).__name__}: {exc}"
          journal.save_collection(replace(record, error=error))
          journal.save(replace(run, collection_error=error))
          raise

  async def collect_fluorescence(
    self, run_id: str, *, timeout: float = 900.0, poll_interval: float = 0.25
  ) -> tuple[FluorescenceCollection, ...]:
    """Service a started optical run until thermal completion and return its saved collections.

    Call immediately after start_run(..., collection_plan=...). Polls use the instrument's hold
    countdown; no host sleep substitutes for thermal timing and no hold is extended. Reads start
    within the plan's window and require the same run/hold before and after every exposure.
    Incoherent status/history observations are reconciled without taking exposures, until fresh
    evidence establishes ownership or the overall timeout expires. A missed point, timeout, task
    cancellation or ambiguous exposure permanently records an optical failure for this run.
    Repeating this call never repeats an uncertain collection. The overall timeout includes cover
    warmup, ramps, every hold, acquisition and completion reconciliation; budget for all of them.

    This does not cancel heating or switch off the lamp on failure. Use cancel_run() for the owned
    thermal run and set_lamp(False) after inactivity; reconnect first after a communication fault.
    Cancellation can acquire the run lock between complete collection bursts. Original and partial
    frames remain accessible through get_collections(). A terminal thermal run alone does not
    establish optical success. Requires explicit hardware-session approval.
    """
    _validate_run_id(run_id)
    if (
      not math.isfinite(timeout)
      or timeout <= 0
      or not math.isfinite(poll_interval)
      or not 0.05 <= poll_interval <= 0.5
    ):
      raise ValueError("Positive timeout and a poll interval of 0.05–0.5 seconds are required")
    journal = self._require_journal()
    run = journal.get(run_id)
    if run is None or run.collection_plan is None:
      raise ABI7500RunError("Start a run with an explicit collection plan first")
    if run.collection_error:
      raise ABI7500RunError(f"Optical run cannot resume: {run.collection_error}")
    plan = run.collection_plan
    points = plan.points(run.program)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
      while True:
        run = await self.get_run(run_id)
        if run.collection_error:
          raise ABI7500RunError(f"Optical run cannot resume: {run.collection_error}")
        records = self._read_collections(run)
        if any(value.record.state != "complete" for value in records):
          raise ABI7500RunError("An uncertain collection cannot be repeated or resumed")
        if run.is_terminal:
          if run.state != "completed" or len(records) != len(points):
            raise ABI7500RunError("Thermal run ended without every planned collection")
          return records
        self._remaining(deadline, context="fluorescence observation (including startup)")
        if run.state == "cancelling":
          raise ABI7500RunError("Cancellation prevents optical collection")
        # A status/history/status read can straddle startup or completion. Keep reconciling an
        # uncertain observation without actuation; only a fresh owned running state permits CC.
        status = run.last_status
        if len(records) < len(points) and run.state == "running" and status is not None:
          point = points[len(records)]
          observed = (status.cycle_index, status.repetition, status.step_index)
          expected = (point.cycle_index, point.repetition, point.step_index)
          if observed > expected:
            raise ABI7500RunError("A planned collection hold was missed")
          if point.matches(status):
            assert status.remaining_hold_time is not None
            if status.remaining_hold_time < plan.read_window - 2:
              raise ABI7500RunError("Collection window was missed")
            if status.remaining_hold_time <= plan.read_window:
              await self._collect_point(run_id, point)
              continue
        await asyncio.sleep(
          min(
            poll_interval,
            self._remaining(deadline, context="fluorescence observation (including startup)"),
          )
        )
    except BaseException as exc:
      async with self._run_lock:
        current = journal.get(run_id)
        if current is not None and current.collection_error is None:
          journal.save(replace(current, collection_error=f"{type(exc).__name__}: {exc}"))
      raise

  async def cancel_run(self, run_id: str, *, timeout: float = 30.0) -> ThermalRun:
    """Stop an identified run, confirm inactivity, and request the captured 25 °C idle target.

    Repeated calls first reconcile state and skip Stop if already inactive. An uncertain active
    run with no ownership evidence is refused. A timeout never implies that the run stopped; use
    stop()/setup() after communication failure, then get_run()/cancel_run() with the same journal.
    This does not eject the plate or cool the cover. A 25 °C setpoint acknowledgement is not proof
    that the sample has reached 25 °C. Python task cancellation only interrupts the host operation.
    """
    _validate_run_id(run_id)
    if not math.isfinite(timeout) or timeout <= 0:
      raise ValueError("timeout must be finite and positive")
    async with self._run_lock:
      journal = self._require_journal()
      run = journal.get(run_id)
      if run is None:
        raise ABI7500RunError(f"Unknown run {run_id!r}")
      if run.is_terminal:
        return run
      await self._check_run_identity(run)
      run = await self._observe_run(run)
      if run.is_terminal:
        return run
      status = run.last_status
      if (
        run.start_record is None
        or status is None
        or status.run_start_record != run.start_record
        or run.state not in ("running", "cancelling")
      ):
        raise ABI7500RunError(
          "Run ownership is uncertain; refusing to stop an unidentified sequence"
        )
      run = journal.save(replace(run, cancel_requested=True, state="cancelling"))
      try:
        logger.info("Cancelling thermal run %s", run_id)
        if status.phase != "inactive":
          await self._exchange(0x07, 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
          status = await self.get_status()
          if status.run_start_record != run.start_record:
            raise ABI7500RunError("Run identity changed during cancellation")
          if status.phase == "inactive" and status.state_code in (1, 2):
            break
          if status.phase == "unknown":
            raise ABI7500RunError("Unrecognized sequence state during cancellation")
          await asyncio.sleep(min(0.5, self._remaining(deadline)))
        await self._exchange(0x22, 0, b"\x00\xfa")
        run = journal.save(replace(run, idle_temperature_set=True, last_status=status))
        return await self._observe_run(run)
      except BaseException:
        journal.save(replace(run, state="uncertain"))
        logger.error(
          "Cancellation of run %s is unresolved; reconcile before further actuation", run_id
        )
        raise

  async def stop(self) -> None:
    """Close USB and stop keepalives; this does not cancel a run or switch off the lamp.

    Safe to call repeatedly. Use cancel_run() for run cancellation;
    disconnecting a control computer is not evidence that an instrument has stopped heating.
    """
    async with self._run_lock, self._lock:
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
        if self._journal is not None:
          self._journal.close()
      logger.info("ABI 7500 USB connection closed")
