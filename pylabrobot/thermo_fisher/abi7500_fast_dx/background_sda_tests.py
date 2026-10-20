"""Check native background precision against captured vendor integers and reject malformed data."""

import base64
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from .fluorescence import BackgroundCalibration, CameraFilterResult
from .fluorescence_tests import _background, _frames, _uniformity

_FIXTURE = json.loads((Path(__file__).parent / "test_data/background_sda_vectors.json").read_text())
_SECTION = zlib.decompress(base64.b64decode(_FIXTURE["section_zlib_base64"]))
_HEADER = bytes.fromhex(_FIXTURE["header_prefix_hex"]).ljust(1024, b"\x00")
_SOURCE = _HEADER + _SECTION


def _replace_at(source: bytes, position: int, replacement: bytes) -> bytes:
  """Overwrite a bounded test field without shifting neighboring records."""
  return source[:position] + replacement + source[position + len(replacement) :]


class BackgroundSdaTests(unittest.TestCase):
  """No instrument, complete calibration backup, or vendor binary is needed."""

  def test_captured_precision_and_filter_order(self) -> None:
    """Read little-endian binary32 pairs and reverse physical filters into saved channels."""
    self.assertEqual(len(_SECTION), _FIXTURE["section_bytes"])
    self.assertEqual(hashlib.sha256(_SECTION).hexdigest(), _FIXTURE["section_sha256"])
    calibration = BackgroundCalibration(_SOURCE)
    self.assertIs(calibration.source_bytes, _SOURCE)
    self.assertEqual(calibration.sha256, hashlib.sha256(_SOURCE).hexdigest())
    self.assertEqual(len(calibration.offsets), 5)
    self.assertTrue(all(len(row) == 96 for row in calibration.offsets + calibration.slopes))
    for physical, pair in enumerate(_FIXTURE["first_well_physical_offset_slope_pairs"]):
      self.assertEqual(calibration.offsets[4 - physical][0], pair[0])
      self.assertEqual(calibration.slopes[4 - physical][0], pair[1])
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "Calibration.sda"
      path.write_bytes(_SOURCE)
      self.assertEqual(BackgroundCalibration.from_file(path), calibration)

  def test_recorded_integer_boundaries(self) -> None:
    """Native coefficients recover three saved vendor counts lost through rounded CSV export."""
    calibration = BackgroundCalibration(_SOURCE)
    for vector in _FIXTURE["vectors"]:
      with self.subTest(cycle=vector["cycle"], well=vector["source_well"]):
        index = vector["source_well"] - 1
        saved = vector["saved_filter"] - 1
        self.assertEqual(calibration.offsets[saved][index], vector["background_offset"])
        self.assertEqual(calibration.slopes[saved][index], vector["background_slope"])
        frames = _frames(vector)
        result = CameraFilterResult(
          frames, calibration, _uniformity(1), 2, vector["cycle"], vector["cycle"]
        )
        self.assertEqual(result.measurements[index].raw_value, vector["raw_value"])
        rounded = _background(vector["background_offset"], vector["csv_slope"])
        rounded_result = CameraFilterResult(
          frames, rounded, _uniformity(1), 2, vector["cycle"], vector["cycle"]
        )
        self.assertEqual(
          rounded_result.measurements[index].raw_value, vector["rounded_csv_raw_value"]
        )
        self.assertEqual(vector["raw_value"], vector["rounded_csv_raw_value"] + 1)

  def test_well_indices_and_opaque_sections(self) -> None:
    """Well numbers determine association; unrelated top-level data is skipped by its length."""
    position = _SOURCE.index(b"CalP") + 10
    records = _SOURCE[position:]
    self.assertEqual(len(records), 96 * 80)
    reversed_records = b"".join(records[i : i + 80] for i in reversed(range(0, len(records), 80)))
    opaque = b"TEST" + struct.pack(">I", 5) + b"CLBl!"
    changed = _HEADER + opaque + _SOURCE[1024:position] + reversed_records + opaque
    expected, actual = BackgroundCalibration(_SOURCE), BackgroundCalibration(changed)
    self.assertEqual(actual.offsets, expected.offsets)
    self.assertEqual(actual.slopes, expected.slopes)

  def test_reject_malformed_or_unsupported_data(self) -> None:
    """Bound lengths, profile, geometry, well coverage and coefficient validity before use."""
    first_well = _SOURCE.index(b"WelN") + 8
    second_well = _SOURCE.index(b"WelN", first_well) + 8
    coefficients = _SOURCE.index(b"WCFA") + 8
    profile = _SOURCE.index(b"InId") + 8
    invalid_sources = (
      _SOURCE[:1023],
      _SOURCE[:-1],
      _SOURCE + b"x",
      _HEADER,
      _SOURCE + _SECTION,
      _replace_at(_SOURCE, 7, b"\x03"),
      _replace_at(_SOURCE, 23, b"\x18"),
      _replace_at(_SOURCE, 1028, struct.pack(">I", len(_SOURCE))),
      _replace_at(_SOURCE, 1032, b"\x00\x00"),
      _replace_at(_SOURCE, profile, b"\x00\x00\x03\x28"),
      _replace_at(_SOURCE, _SOURCE.index(b"CPRM") + 8, b"\x01"),
      _replace_at(_SOURCE, _SOURCE.index(b"XTak") + 8, b"\x02"),
      _replace_at(_SOURCE, first_well, b"\x00\x60"),
      _replace_at(_SOURCE, second_well, b"\x00\x00"),
      _replace_at(_SOURCE, _SOURCE.index(b"CaWe"), b"NONE"),
      _replace_at(_SOURCE, _SOURCE.index(b"TSMP"), b"WelN"),
      _replace_at(_SOURCE, coefficients - 4, struct.pack(">I", 39)),
      _replace_at(_SOURCE, coefficients, struct.pack("<f", float("nan"))),
      _replace_at(_SOURCE, coefficients + 4, struct.pack("<f", float("inf"))),
      _SOURCE + bytes(1024 * 1024),
    )
    for index, invalid in enumerate(invalid_sources):
      with self.subTest(case=index), self.assertRaises(ValueError):
        BackgroundCalibration(invalid)
