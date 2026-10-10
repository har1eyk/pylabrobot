"""Uniformity precision and provenance compared with the original vendor Java implementation."""

import hashlib
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from .calibration import UniformityCalibration, apply_uniformity, read_eds_uniformity
from .results import SavedFilterData

_VECTOR = json.loads((Path(__file__).parent / "test_data/uniformity_vectors.json").read_text())


def _source() -> bytes:
  """Build synthetic format-valid calibration data with distinct factors for every well/filter."""
  text = (
    "[head]\nversion=1.0.0\ntimestamp=1770832404129\n"
    "[instrument]\nid=null\ntype=sds7500fast\n[scanner]\nid=*\nfilters=1,2,3,4,5\n"
    "[block]\ntype=96fast\nrows=8\ncolumns=12\n[result]\nresult=true\n[uniformity]\n"
  )
  for index, factors in enumerate(_VECTOR["factors"], 1):
    text += f"{index}=<<eof\n"
    text += "\n".join(",".join(factors[start : start + 12]) for start in range(0, 96, 12))
    text += "\neof\n"
  return text.encode()


class UniformityTests(unittest.TestCase):
  """Independent float bits detect transposition, wrong factors and premature float64 rounding."""

  def test_original_java_oracle_bits_and_original_integer_preservation(self) -> None:
    """All 480 corrected values match the vendor, including signed int64 rounding boundaries."""
    calibration = UniformityCalibration(_source())
    bits: list[str] = []
    for filter_index in range(1, 6):
      source = SavedFilterData.decode(
        f"apldbio/sds/images/stage2-cycle003-point1-{filter_index}.tiff",
        struct.pack(">96q", *_VECTOR["values"]),
      )
      result = apply_uniformity(source, calibration)
      self.assertIs(result.source, source)
      self.assertIs(result.calibration, calibration)
      self.assertEqual(result.measurements, result.measurements)
      self.assertEqual([v.raw_value for v in result.measurements], _VECTOR["values"])
      bits.extend(struct.pack(">f", v.corrected_value).hex() for v in result.measurements)
      self.assertEqual(
        [result.measurements[i].label for i in (0, 11, 12, 95)], ["A1", "A12", "B1", "H12"]
      )
    self.assertEqual("".join(bits), _VECTOR["expected_float32_hex"])

  def test_source_provenance_and_line_endings(self) -> None:
    """Byte hashes distinguish files while coefficients remain stable across CRLF conversion."""
    raw = _source()
    a, b = UniformityCalibration(raw), UniformityCalibration(raw.replace(b"\n", b"\r\n"))
    self.assertEqual(a.coefficients, b.coefficients)
    self.assertEqual(a.source_bytes, raw)
    self.assertEqual(a.sha256, hashlib.sha256(raw).hexdigest())
    self.assertNotEqual(a.sha256, b.sha256)
    self.assertEqual(a.timestamp, 1770832404129)
    self.assertEqual((a.instrument_id, a.scanner_id), ("null", "*"))

  def test_failed_wrong_platform_and_malformed_calibrations(self) -> None:
    """Failed, incompatible, truncated, duplicated and nonfinite factors are rejected."""
    raw = _source()
    first = _VECTOR["factors"][0][0].encode()
    bad = [
      raw.replace(b"result=true", b"result=false"),
      raw.replace(b"sds7500fast", b"sds7300"),
      raw.replace(b"rows=8", b"rows=12"),
      raw.replace(b"filters=1,2,3,4,5", b"filters=5,4,3,2,1"),
      raw.replace(first, b"nan"),
      raw.replace(first, b"1e99"),
      raw.replace(first, b"0"),
      raw.replace(first, b"-1"),
      raw.replace(first + b",", b""),
      raw[:-4],
      raw + b"[head]\nversion=1.0.0\n",
      raw.replace(b"rows=8", b"rows=8\nrows=8"),
      raw.replace(b"timestamp=1770832404129", b"timestamp=-1"),
    ]
    for source in bad:
      with self.subTest(source=source[:120]):
        with self.assertRaises(ValueError):
          UniformityCalibration(source)

  def test_eds_association_and_duplicate_refusal(self) -> None:
    """Use only the experiment's single saved calibration, without archive extraction."""
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "experiment.eds"
      with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("APLDBIO\\SDS\\CALIBRATIONS/uniformity.ini", _source())
        archive.writestr("../never-extracted", b"data")
      self.assertEqual(read_eds_uniformity(path).source_bytes, _source())
      self.assertEqual(list(Path(directory).iterdir()), [path])
      with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("apldbio/sds/calibrations/uniformity.ini", _source())
      with self.assertRaisesRegex(ValueError, "exactly one"):
        read_eds_uniformity(path)
      with zipfile.ZipFile(path, "w"):
        pass
      with self.assertRaisesRegex(ValueError, "exactly one"):
        read_eds_uniformity(path)
