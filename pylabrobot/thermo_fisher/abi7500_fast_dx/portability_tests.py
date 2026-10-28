"""Stress acquisition with synthetic host delays and real disposable FULL SQLite journals.

These tests model bounded host behavior; they are not computer or instrument benchmarks.
Every transport is replaced before use, and the thermal countdown advances with host time.
"""

import asyncio
import math
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, cast
from unittest.mock import Mock, patch

import pytest

from pylabrobot.io.usb import USB

from .acquisition import CameraAcquisition, CameraExposure
from .acquisition_tests import _IMAGE
from .cadence_tests import _CadenceUSB, _Clock, _plan, _status
from .camera import CameraGeometry
from .collection import CollectionFilter, CollectionPlan, CollectionPoint
from .collection_tests import _run
from .driver import ABI7500FastDx
from .protocol import ThermalPacket
from .run import ABI7500RunError
from .run_tests import _RUN_ID

_POINT = CollectionPoint(1, 1, 1)


@dataclass(frozen=True)
class _Host:
  """Explicit injected seconds, independent of the machine executing the tests."""

  name: str
  commit: float
  transfer: float
  controller: float
  read_overhead: float


_HOSTS = (
  _Host("low-latency", 0.008, 0.327, 0.060, 0.0001),
  _Host("measured-slow-checkpoint", 0.065, 0.350, 0.060, 0.0005),
  _Host("slow-storage-and-usb", 0.250, 0.750, 0.120, 0.001),
)


class _HostUSB(_CadenceUSB):
  """Byte-scripted transport with a live countdown and configurable response fragmentation."""

  def __init__(self, clock: _Clock, host: _Host, fragment: int, plan: CollectionPlan) -> None:
    """Keep the injected delays and packet boundaries separate from captured image bytes."""
    super().__init__(clock)
    self.host = host
    self.fragment = fragment
    self.plan = plan
    self.hold_origin: Optional[float] = None
    self.hold_repetition: Optional[int] = None
    self.image_reads = 0
    self.status_reads = 0
    self.pause_at_image: Optional[int] = None
    self.pause_duration = 0.0

  def image(self) -> None:
    """Return short/odd fragments even when the host requests larger buffers."""
    chunk_size = 4096 if self.plan.cadence is None else 16384
    offset = 0
    while offset < len(_IMAGE):
      requested = min(chunk_size, len(_IMAGE) - offset)
      size = min(requested, self.fragment)
      self.expect_read(_IMAGE[offset : offset + size], requested)
      offset += size

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Inject host/controller cost while preserving the exact expected command sequence."""
    await super().write(data, timeout)
    if data.startswith(b"RS"):
      self.clock.now += self.host.controller - (0.11 if self.last_command == 0x29 else 0.06)

  async def read(self, size: int, timeout: Optional[int] = None) -> bytes:
    """Advance countdown across all host stalls; preserve original camera payload bytes."""
    reading = self.reading_image
    result = await super().read(size, timeout)
    if reading:
      self.image_reads += 1
      self.clock.now += (self.host.transfer - 0.327) * len(result) / len(_IMAGE)
      self.clock.now += self.host.read_overhead
      if not self.reading_image and self.pause_at_image == len(self.starts):
        self.clock.now += self.pause_duration
    if self.last_command == 0x29 and size == 73:
      self.status_reads += 1
      packet = ThermalPacket.decode(result[2:])
      if self.hold_origin is None or self.hold_repetition != packet.payload[31]:
        self.hold_origin = self.clock.now
        self.hold_repetition = packet.payload[31]
      remaining = max(0, self.plan.read_window - math.ceil(self.clock.now - self.hold_origin))
      payload = bytearray(packet.payload)
      payload[20:24] = remaining.to_bytes(4, "big")
      wire = replace(packet, payload=bytes(payload)).encode()
      result = len(wire).to_bytes(2, "little") + wire
    return result

  def ordinary_point(self, repetition: int = 1) -> None:
    """Script a whole fixed-image collection with a fresh status around every image."""
    before = _status(repetition, remaining=self.plan.read_window)
    self.observe(before)
    self.optics(0 if repetition == 1 else self.plan.filters[-1].position)
    self.expect_write(b"CH   5")
    self.expect_read(b"ready")
    for setting in self.plan.filters:
      self.exchange(0x29, before.raw_payload)
      self.exchange(0x69, request=setting.position.to_bytes(2, "big"))
      self.exchange(0x6C, b"\0\1")
      self.exchange(0x6C, b"\0\0")
      self.optics(setting.position)
      for duration in setting.exposures:
        exposure = CameraExposure(duration)
        self.expect_write(exposure.encode())
        self.expect_read(exposure.milliseconds.to_bytes(2, "little"))
        self.exchange(0x29, before.raw_payload)
        self.expect_write(b"CC")
        self.expect_write(b"CH   5")
        self.expect_read(b"ready")
        self.expect_write(CameraGeometry(321, 245).encode_read())
        self.image()
        self.optics(setting.position)
        self.exchange(0x29, before.raw_payload)
    self.exchange(0x29, before.raw_payload)


class _Scenario:
  """Own one disposable journal and fake USB transport without instrument discovery."""

  def __init__(
    self, root: Path, host: _Host, fragment: int, plan: CollectionPlan, repetitions: int = 1
  ) -> None:
    """Reserve a synthetic active run and add only deterministic latency to real commits."""
    self.path = root / "runs.sqlite3"
    self.driver = ABI7500FastDx(run_journal=self.path)
    self.clock = _Clock()
    self.usb = _HostUSB(self.clock, host, fragment, plan)
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.journal = self.driver._require_journal()
    run = _run()
    program = replace(
      run.program,
      cycles=tuple(
        replace(
          c,
          repetitions=repetitions,
          steps=tuple(replace(s, hold_time=plan.read_window + 100) for s in c.steps),
        )
        for c in run.program.cycles
      ),
    )
    self.journal.reserve(replace(run, program=program, collection_plan=plan))
    if plan.cadence is None:
      for repetition in range(1, repetitions + 1):
        self.usb.ordinary_point(repetition)
    else:
      self.usb.burst_point(plan)
    self.journal._open().set_trace_callback(self.charge_commit)

  def charge_commit(self, statement: str) -> None:
    """Add latency without removing or substituting SQLite's actual FULL commit."""
    if statement == "COMMIT":
      self.clock.now += self.usb.host.commit

  async def collect(self, point: CollectionPoint = _POINT) -> None:
    """Use virtual monotonic time for driver deadlines while real asyncio handles tasks."""
    with (
      patch(
        "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.get_running_loop",
        return_value=self.clock,
      ),
      patch("pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=self.clock.sleep),
    ):
      await self.driver._collect_point(_RUN_ID, point)

  async def verify_saved(self, *, complete: bool) -> None:
    """Check exact bytes, independent durability, and refusal to replay after reopen."""
    collection = (await self.driver.get_collections(_RUN_ID))[0]
    assert collection.record.state == ("complete" if complete else "uncertain")
    received = sum(bool(a.wire_data) for a in collection.acquisitions)
    assert received == len(self.usb.starts)
    assert all(a.wire_data == _IMAGE for a in collection.acquisitions if a.wire_data)
    with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
      assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert self.journal._open().execute("PRAGMA synchronous").fetchone() == (2,)
    writes = list(self.usb.writes)
    if complete:
      assert received == sum(len(f.exposures) for f in self.usb.plan.filters)
      assert all(a.state == "complete" for a in collection.acquisitions)
      await self.collect()
    else:
      with pytest.raises(ABI7500RunError):
        await self.collect()
    assert self.usb.writes == writes
    await self.driver.stop()
    self.driver = ABI7500FastDx(run_journal=self.path)
    assert (await self.driver.get_collections(_RUN_ID))[0] == collection
    if complete:
      await self.driver._collect_point(_RUN_ID, _POINT)
    else:
      with pytest.raises(ABI7500RunError):
        await self.driver.collect_fluorescence(_RUN_ID)


@pytest.mark.parametrize("host", _HOSTS, ids=lambda h: h.name)
@pytest.mark.parametrize("fragment", (16384, 4096, 511))
def test_ordinary_65_images_across_host_delays(host: _Host, fragment: int) -> None:
  """Bounded slow hosts finish all images without imposing vendor cadence on ordinary plans."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      plan = replace(_plan(True), cadence=None, read_window=600)
      scenario = _Scenario(Path(root), host, fragment, plan)
      try:
        await scenario.collect()
        assert not scenario.usb.script
        assert len(scenario.usb.starts) == 65
        assert scenario.usb.status_reads >= 130
        await scenario.verify_saved(complete=True)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


@pytest.mark.parametrize("fragment", (16384, 4096, 511))
def test_tight_cadence_rejects_slow_host_without_losing_frames(fragment: int) -> None:
  """A host outside the comparison budget preserves evidence and never silently relaxes it."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      scenario = _Scenario(Path(root), _HOSTS[2], fragment, _plan(True))
      try:
        with pytest.raises(ABI7500RunError, match="cadence deadline"):
          await scenario.collect()
        assert 0 < len(scenario.usb.starts) < 65
        await scenario.verify_saved(complete=False)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


@pytest.mark.parametrize("host", _HOSTS, ids=lambda h: h.name)
def test_reference_hold_distinguishes_feasible_and_excessive_host_delays(host: _Host) -> None:
  """The actual 205-second window must not silently expand to accommodate a slow host."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      scenario = _Scenario(Path(root), host, 511, replace(_plan(True), cadence=None))
      try:
        if host.name == "slow-storage-and-usb":
          with pytest.raises(ABI7500RunError, match="hold|thermal"):
            await scenario.collect()
          assert 0 < len(scenario.usb.starts) < 65
          await scenario.verify_saved(complete=False)
        else:
          await scenario.collect()
          assert not scenario.usb.script
          assert len(scenario.usb.starts) == 65
          await scenario.verify_saved(complete=True)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


@pytest.mark.parametrize("pause", (0.6, 3.0))
def test_ordinary_acquisition_tolerates_scheduler_pause(pause: float) -> None:
  """A host pause longer than vendor tolerance is acceptable inside an ordinary hold budget."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      plan = replace(_plan(), cadence=None, read_window=205)
      scenario = _Scenario(Path(root), _HOSTS[1], 511, plan)
      scenario.usb.pause_at_image = 8
      scenario.usb.pause_duration = pause
      try:
        await scenario.collect()
        assert not scenario.usb.script
        await scenario.verify_saved(complete=True)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


def test_disk_stall_after_status_cannot_start_exposure_in_expired_hold() -> None:
  """A status checkpoint stalled past the hold must prevent CC, even with a valid old reply."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      plan = replace(_plan(), cadence=None)
      scenario = _Scenario(Path(root), _HOSTS[0], 4096, plan)
      save = scenario.journal.save_acquisition
      saves = 0

      def delayed_save(acquisition: CameraAcquisition) -> CameraAcquisition:
        """Stall only the first thermal-before checkpoint after it commits."""
        nonlocal saves
        result = save(acquisition)
        saves += 1
        if saves == 2:
          scenario.clock.now += plan.read_window
        return result

      try:
        with patch.object(scenario.journal, "save_acquisition", side_effect=delayed_save):
          with pytest.raises(ABI7500RunError):
            await scenario.collect()
        assert not scenario.usb.starts
        await scenario.verify_saved(complete=False)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


def test_move_acknowledgement_delay_does_not_restart_movement_budget() -> None:
  """Time spent awaiting a move acknowledgement belongs to the original movement budget."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      scenario = _Scenario(Path(root), _HOSTS[0], 4096, replace(_plan(), cadence=None))
      query = scenario.driver._thermal_query

      async def stalled_query(command: int, size: int, payload: bytes = b"") -> bytes:
        """Model host scheduling after a successful move reply has been received."""
        reply = await query(command, size, payload)
        if command == 0x69:
          scenario.clock.now += 16
        return reply

      try:
        with patch.object(scenario.driver, "_thermal_query", side_effect=stalled_query):
          with pytest.raises(TimeoutError):
            await scenario.collect()
        assert not scenario.usb.starts
        assert scenario.usb.last_command == 0x69
        await scenario.verify_saved(complete=False)
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


@pytest.mark.parametrize("host", _HOSTS, ids=lambda h: h.name)
def test_forty_pcr_collections_keep_cycles_and_frames_on_slow_hosts(host: _Host) -> None:
  """Service forty synthetic holds with two physical filters and three exposures each."""

  async def run() -> None:
    with tempfile.TemporaryDirectory() as root:
      plan = CollectionPlan(
        ((1, 1),),
        (CollectionFilter(2, (0.540, 0.180, 0.060)), CollectionFilter(5, (0.540, 0.180, 0.060))),
        read_window=45,
      )
      scenario = _Scenario(Path(root), host, 511, plan, repetitions=40)
      try:
        for repetition in range(1, 41):
          await scenario.collect(replace(_POINT, repetition=repetition))
        assert not scenario.usb.script
        assert len(scenario.usb.starts) == 240
        collections = await scenario.driver.get_collections(_RUN_ID)
        assert len(collections) == 40
        ids = set()
        for repetition, collection in enumerate(collections, 1):
          assert collection.record.state == "complete"
          assert collection.record.point.repetition == repetition
          assert len(collection.acquisitions) == 6
          for acquisition in collection.acquisitions:
            assert acquisition.state == "complete"
            assert acquisition.wire_data == _IMAGE
            assert acquisition.observation_scope == "frame"
            assert acquisition.thermal_before.repetition == repetition
            assert acquisition.thermal_after is not None
            assert acquisition.thermal_after.repetition == repetition
            ids.add(acquisition.acquisition_id)
        assert len(ids) == 240
        writes = list(scenario.usb.writes)
        await scenario.collect(replace(_POINT, repetition=40))
        assert scenario.usb.writes == writes
        await scenario.driver.stop()
        scenario.driver = ABI7500FastDx(run_journal=scenario.path)
        assert await scenario.driver.get_collections(_RUN_ID) == collections
      finally:
        await scenario.driver.stop()

  asyncio.run(run())


@pytest.mark.parametrize("chunk_size", (4096, 16384))
@pytest.mark.parametrize("fragment", (64, 511, 4096, 16384))
def test_image_reader_through_real_usb_executor(chunk_size: int, fragment: int) -> None:
  """Exercise USB.read packet aggregation and executor use, without discovering a device."""

  async def run() -> None:
    driver = ABI7500FastDx()
    io = driver.io
    device = Mock()
    endpoint = SimpleNamespace(wMaxPacketSize=64)
    io.dev = device
    io.read_endpoint = endpoint  # type: ignore[assignment]
    io.write_endpoint = endpoint  # type: ignore[assignment]
    phase = ""
    remaining = b""
    commands: list[bytes] = []

    def write(endpoint: object, data: bytes, timeout: int) -> int:
      """Expose a camera response only after its matching command has been submitted."""
      nonlocal phase, remaining
      assert not remaining
      commands.append(data)
      if data == b"CH   5":
        phase, remaining = "ready", b"ready"
      else:
        assert data == CameraGeometry(321, 245).encode_read()
        phase, remaining = "image", _IMAGE
      return len(data)

    def read(endpoint: object, size: int, timeout: int) -> bytes:
      """Supply whole or short libusb responses, respecting every requested maximum length."""
      nonlocal remaining
      assert remaining
      assert 0 < size <= (chunk_size if phase == "image" else 5)
      count = min(size, fragment, len(remaining))
      result, remaining = remaining[:count], remaining[count:]
      return result

    device.write.side_effect = write
    device.read.side_effect = read
    data = bytearray()
    readiness: list[bytes] = []
    with ThreadPoolExecutor(max_workers=1) as reader, ThreadPoolExecutor(max_workers=1) as writer:
      io._read_executor, io._write_executor = reader, writer
      try:
        await driver._read_camera_image(
          CameraExposure(0.256), CameraGeometry(321, 245), data, readiness, chunk_size=chunk_size
        )
      finally:
        io.dev = None
        io._read_executor = io._write_executor = None
    assert bytes(data) == _IMAGE
    assert readiness == [b"ready"]
    assert not remaining
    assert commands == [b"CH   5", CameraGeometry(321, 245).encode_read()]

  asyncio.run(run())
