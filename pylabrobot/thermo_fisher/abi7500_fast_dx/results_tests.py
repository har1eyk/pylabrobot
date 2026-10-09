"""Saved filter data tests against the vendor's independent live result log."""

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from pylabrobot.thermo_fisher.abi7500_fast_dx.protocol import ABI7500ProtocolError
from pylabrobot.thermo_fisher.abi7500_fast_dx.results import SavedFilterData, read_eds_filter_data

_VECTOR = json.loads((Path(__file__).parent / "test_data" / "saved_filter_vector.json").read_text())
_DATA = bytes.fromhex(_VECTOR["data_hex"])


class SavedFilterDataTests(unittest.TestCase):
  def test_saved_bytes_match_live_vendor_event(self) -> None:
    """All 96 original integers match the separate live result message."""
    result = SavedFilterData.decode(_VECTOR["member"], _DATA)
    self.assertEqual(result.source_bytes, _DATA)
    self.assertEqual(list(result.well_values), _VECTOR["expected_values"])
    self.assertEqual(
      (result.stage, result.cycle, result.collection_point, result.filter), (2, 1, 1, 1)
    )

  def test_signed_values_and_precision(self) -> None:
    """The format is signed 64-bit, including values not exactly representable by float64."""
    values = (-(2**63), -1, 0, 2**53 + 1, 2**63 - 1) + (0,) * 91
    result = SavedFilterData.decode(_VECTOR["member"], struct.pack(">96q", *values))
    self.assertEqual(result.well_values, values)

  def test_invalid_member_names_and_lengths(self) -> None:
    """Misleading TIFF names and malformed arrays must fail explicitly."""
    for name in ("frame.tiff", _VECTOR["member"].replace("-1.tiff", "-6.tiff")):
      with self.assertRaises(ABI7500ProtocolError):
        SavedFilterData.decode(name, _DATA)
    with self.assertRaises(ValueError):
      SavedFilterData.decode(_VECTOR["member"].replace("stage2", "stage0"), _DATA)
    for data in (b"", _DATA[:-1], _DATA + b"\x00"):
      with self.assertRaises(ABI7500ProtocolError):
        SavedFilterData.decode(_VECTOR["member"], data)

  def test_archive_sorting_and_unrelated_members(self) -> None:
    """Archive storage order need not be acquisition order; unrelated files are not extracted."""
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "test.eds"
      names = [
        "apldbio/sds/images/stage2-cycle002-point2-1.tiff",
        "apldbio/sds/images/stage2-cycle001-point1-5.tiff",
        _VECTOR["member"],
      ]
      with zipfile.ZipFile(path, "w") as archive:
        for name in names:
          archive.writestr(name, _DATA)
        archive.writestr("../not-extracted.txt", b"unrelated content")
      results = read_eds_filter_data(path)
      self.assertEqual([result.source_member for result in results], list(reversed(names)))
      self.assertEqual(list(Path(directory).iterdir()), [path])

  def test_empty_cancelled_experiment(self) -> None:
    """A run stopped before collection has no arrays; do not fabricate zeros."""
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "stopped.eds"
      with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("apldbio/sds/experiment.xml", b"<Experiment/>")
      self.assertEqual(read_eds_filter_data(path), ())

  def test_duplicate_indices_and_invalid_archive_member(self) -> None:
    """Alternate zero-padding must not disguise duplicate acquisition indices."""
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "duplicate.eds"
      with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_VECTOR["member"], _DATA)
        archive.writestr(_VECTOR["member"].replace("cycle001", "cycle1"), _DATA)
      with self.assertRaisesRegex(ABI7500ProtocolError, "Duplicate"):
        read_eds_filter_data(path)
      path = Path(directory) / "invalid.eds"
      with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_VECTOR["member"], _DATA + b"\x00")
      with self.assertRaises(ABI7500ProtocolError):
        read_eds_filter_data(path)
