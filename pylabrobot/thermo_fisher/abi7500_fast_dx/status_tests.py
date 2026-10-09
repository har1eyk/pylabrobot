"""Check status fields against independently logged vendor temperatures and run times."""

import json
import unittest
from pathlib import Path

from .protocol import ABI7500ProtocolError
from .status import ABI7500Identity, ABI7500Status

_DATA = Path(__file__).parent / "test_data"


class StatusTests(unittest.TestCase):
  """Captured status must agree with the vendor's human-readable polling log."""

  def test_captured_temperatures_and_elapsed_time(self) -> None:
    """Cover startup, heating, holding, both cycles and cancellation-session heating."""
    vectors = json.loads((_DATA / "status_vectors.json").read_text())["vectors"]
    for vector in vectors:
      with self.subTest(session=vector["session"], sequence=vector["reply"]["sequence"]):
        status = ABI7500Status(bytes.fromhex(vector["reply"]["payload_hex"]))
        expected = vector["expected"]
        self.assertEqual(status.elapsed_time, expected["elapsed_time"])
        self.assertEqual(status.sample_temperature, expected["sample_temperature"])
        self.assertEqual(status.block_temperature, expected["block_temperature"])
        self.assertEqual(status.cover_temperature, expected["cover_temperature"])
        self.assertEqual(status.ambient_temperature, expected["ambient_temperature"])

  def test_native_phase_and_inactive_time_sentinel(self) -> None:
    """Preserve unknown states rather than accidentally presenting them as idle."""
    raw = bytearray(50)
    raw[1] = 255
    raw[20:24] = b"\xff" * 4
    raw[28:32] = bytes([1, 2, 2, 3])
    status = ABI7500Status(bytes(raw))
    self.assertEqual(status.state_code, 255)
    self.assertEqual(status.phase, "holding")
    self.assertIsNone(status.remaining_hold_time)
    self.assertEqual((status.cycle_index, status.step_index, status.repetition), (1, 2, 3))
    raw[30] = 255
    self.assertEqual(ABI7500Status(bytes(raw)).phase, "unknown")
    for length in (0, 49, 51):
      with self.assertRaises(ABI7500ProtocolError):
        ABI7500Status(bytes(length))

  def test_identity_stops_at_nul_and_preserves_tails(self) -> None:
    """Opaque bytes after the C strings must not be decoded as extra serial numbers."""
    vector = json.loads((_DATA / "identity_vector.json").read_text())
    camera = bytes.fromhex(vector["camera_hex"])
    thermal = bytes.fromhex(vector["thermal"]["reply"]["payload_hex"])
    identity = ABI7500Identity(camera, thermal)
    self.assertEqual(identity.camera_description, "Hamamatsu,C9254-03,2.20,1.05,2.19,007006")
    self.assertEqual(identity.thermal_version, "G2.10")
    self.assertEqual(identity.camera_response, camera)
    self.assertEqual(identity.thermal_response, thermal)
    for invalid_camera in (camera[:-1], b"x" * 64, b"\xff\x00" + bytes(62)):
      with self.assertRaises(ABI7500ProtocolError):
        ABI7500Identity(invalid_camera, thermal)

  def test_inactive_sequence_can_have_advancing_time_and_hot_cover(self) -> None:
    """Real inactive replies retain hold time and a hot cover while elapsed time advances."""
    fixture = json.loads((_DATA / "ubuntu_observation_vectors.json").read_text())
    statuses = [
      ABI7500Status(bytes.fromhex(raw)) for raw in fixture["inactive_status_payloads_hex"]
    ]
    self.assertEqual(
      [status.elapsed_time for status in statuses], [11557, 11558, 11559, 11561, 11562]
    )
    self.assertEqual(
      [status.cover_temperature for status in statuses], [105.3, 105.2, 105.1, 104.9, 104.8]
    )
    for status in statuses:
      self.assertEqual(status.phase, "inactive")
      self.assertEqual(status.sample_temperature, 25.0)
      self.assertEqual(status.remaining_hold_time, 14.0)
