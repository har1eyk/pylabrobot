"""Captured C9254-02/2.19 traffic and rejection of unverified optical identities."""

import base64
import hashlib
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from pylabrobot.io.usb import USB

from .acquisition_tests import _CameraUSB
from .driver import ABI7500FastDx
from .optics_tests import _OpticalUSB
from .protocol import ThermalPacket
from .run import ABI7500RunError

_VECTOR = json.loads((Path(__file__).parent / "test_data/c9254_02_vectors.json").read_text())
_CAMERA = bytes.fromhex(_VECTOR["camera_hex"])
_VERSION = bytes.fromhex(_VECTOR["thermal_hex"])


class OlderCameraTests(unittest.IsolatedAsyncioTestCase):
  """Exercise the public APIs with strict fake USB and an isolated durable journal."""

  async def asyncSetUp(self) -> None:
    """Install fake transport before any connection or device operation is possible."""
    self.temp = tempfile.TemporaryDirectory()
    self.driver = ABI7500FastDx(run_journal=Path(self.temp.name) / "runs.sqlite3")
    self.usb = _CameraUSB()
    self.driver.io = cast(USB, self.usb)
    self.driver._connected = True
    self.sleep = patch(
      "pylabrobot.thermo_fisher.abi7500_fast_dx.driver.asyncio.sleep", new=AsyncMock()
    )
    self.sleep.start()

  async def asyncTearDown(self) -> None:
    """Close the isolated journal and remove patches even after a failed replay."""
    self.sleep.stop()
    await self.driver.stop()
    self.temp.cleanup()

  async def test_vendor_frame_with_fragmentation_and_duplicate_suppression(self) -> None:
    """The older camera's CJ/CC/CH/CD sequence preserves the native-copy image byte for byte."""
    self.usb.preflight(camera=_CAMERA, version=_VERSION)
    for transfer in _VECTOR["frame_sequence"]:
      data = bytes.fromhex(transfer["hex"])
      if transfer["action"] == "write":
        self.usb.expect_write(data)
      else:
        self.usb.expect_read(data)
    image = zlib.decompress(base64.b64decode(_VECTOR["image_zlib_base64"]))
    self.usb.expect_read(image[:7], 4096)
    for offset in range(7, len(image), 4096):
      size = min(4096, len(image) - offset)
      self.usb.expect_read(image[offset : offset + size], size)
    self.usb.final()
    result = await self.driver.capture_frame("older-camera", exposure=0.256)
    self.assertEqual(result.state, "complete")
    self.assertEqual(result.camera_description, "Hamamatsu,C9254-02,2.19,1.05,2.19,1Y0747")
    self.assertEqual(hashlib.sha256(result.wire_data).hexdigest(), _VECTOR["wire_sha256"])
    assert result.frame is not None
    self.assertEqual(
      hashlib.sha256(result.frame.image_bytes).hexdigest(),
      _VECTOR["reference_copy"]["output_sha256"],
    )
    self.assertEqual(await self.driver.capture_frame("older-camera", exposure=0.256), result)
    self.assertEqual(self.usb.writes.count(b"CC"), 1)
    self.assertFalse(self.usb.script)

  async def test_older_camera_optical_control(self) -> None:
    """Filter and lamp mutations accept this exact camera/firmware pair after normal preflight."""
    usb = _OpticalUSB()
    self.driver.io = cast(USB, usb)
    usb.preflight(position=0, camera=_CAMERA, version=_VERSION)
    usb.exchange(0x69, b"", b"\x00\x05")
    usb.exchange(0x6C, b"\x00\x00")
    usb.final(position=5)
    self.assertEqual((await self.driver.set_filter_position(5)).filter_position, 5)
    usb.preflight(position=5, camera=_CAMERA, version=_VERSION)
    usb.exchange(0x65, b"", b"\x00\x01")
    usb.final(position=5, lamp=b"\x00\x01\x00\x01")
    self.assertEqual((await self.driver.set_lamp(True, power=1)).lamp_power, 1)
    self.assertFalse(usb.script)

  async def test_captured_optical_packets(self) -> None:
    """Replay the second instrument's exact optical framing independently of synthetic envelopes."""
    usb = _OpticalUSB()
    self.driver.io = cast(USB, usb)
    for pair in _VECTOR["optical_exchanges"]:
      request = ThermalPacket.decode(bytes.fromhex(pair["request"]["packet_hex"]))
      reply = ThermalPacket.decode(bytes.fromhex(pair["reply"]["packet_hex"]))
      usb.sequence = self.driver._sequence = request.sequence - 1
      usb.exchange(request.command, reply.payload, request.payload, reply.error_code)
      self.assertEqual(
        await self.driver._exchange(request.command, len(reply.payload), request.payload),
        reply.payload,
      )
    self.assertFalse(usb.script)

  async def test_unknown_model_or_version_rejected_before_mutation(self) -> None:
    """Other model/version combinations cannot inherit permission from the two captured pairs."""
    for camera, version in (
      (_CAMERA.replace(b"2.19", b"2.18", 1), _VERSION),
      (_CAMERA.replace(b"2.19", b"2.20", 1), _VERSION),
      (_CAMERA.replace(b"C9254-02", b"C9254-03"), _VERSION),
      (_CAMERA.replace(b"C9254-02", b"C9254-01"), _VERSION),
      (_CAMERA, _VERSION.replace(b"G2.10", b"G2.11")),
    ):
      with self.subTest(camera=camera, version=version):
        self.usb.preflight(camera=camera, version=version)
        with self.assertRaises(ABI7500RunError):
          await self.driver.capture_frame("unknown-camera")
        self.assertFalse(self.usb.script)
        usb = _OpticalUSB()
        usb.sequence = self.driver._sequence
        self.driver.io = cast(USB, usb)
        usb.preflight(camera=camera, version=version)
        with self.assertRaises(ABI7500RunError):
          await self.driver.set_filter_position(5)
        self.assertFalse(usb.script)
        self.assertFalse({0x64, 0x65, 0x69} & {c for c, _ in usb.commands})
        self.driver.io = cast(USB, self.usb)
        self.usb.sequence = self.driver._sequence
    self.assertNotIn(b"CC", self.usb.writes)

  async def test_older_camera_does_not_relax_geometry_check(self) -> None:
    """A supported identity with unexpected dimensions remains blocked before exposure."""
    self.usb.preflight(camera=_CAMERA, version=_VERSION, geometry=b"\x42\x01\xf5\x00")
    with self.assertRaisesRegex(ABI7500RunError, "geometry"):
      await self.driver.capture_frame("wrong-geometry")
    self.assertNotIn(b"CC", self.usb.writes)
    self.assertFalse(self.usb.script)
