"""Read the saved 96-well filter arrays in reference 7500 EDS experiments.

These arrays are vendor outputs, not camera images, despite their .tiff extension. Reading them
does not reproduce the vendor's optical processing or establish valid calibration.
"""

import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Union

from .protocol import ABI7500ProtocolError

_FILTER_MEMBER = re.compile(r"apldbio/sds/images/stage(\d+)-cycle(\d+)-point(\d+)-([1-5])\.tiff")
_WELL_ARRAY = struct.Struct(">96q")


@dataclass(frozen=True)
class SavedFilterData:
  """One vendor result array, preserving integer precision and its original bytes.

  Stage, cycle, collection point, and filter are the one-based numbers in the EDS member name.
  ``well_values`` follows vendor well indices 1 through 96. Physical plate orientation and dye
  identity are not inferred. Values may already include native optical processing; they must not
  be labeled uncorrected pixel sums or calibrated fluorescence without further validation.
  """

  stage: int
  cycle: int
  collection_point: int
  filter: int
  source_member: str
  source_bytes: bytes

  def __post_init__(self) -> None:
    """Require the recovered member-name structure and complete immutable well data."""
    match = _FILTER_MEMBER.fullmatch(self.source_member)
    if match is None:
      raise ABI7500ProtocolError(f"Unsupported filter member name: {self.source_member}")
    indices = (self.stage, self.cycle, self.collection_point, self.filter)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in indices):
      raise ValueError("Saved filter indices must be positive integers")
    if indices != tuple(int(value) for value in match.groups()):
      raise ValueError("Saved filter indices do not match the source member")
    if not isinstance(self.source_bytes, bytes):
      raise ValueError("source_bytes must be immutable bytes")
    if len(self.source_bytes) != _WELL_ARRAY.size:
      raise ABI7500ProtocolError("Expected exactly 768 bytes for 96 saved well values")

  @property
  def well_values(self) -> Tuple[int, ...]:
    """Return signed 64-bit vendor values as Python integers without precision loss."""
    return _WELL_ARRAY.unpack(self.source_bytes)

  @classmethod
  def decode(cls, member: str, data: bytes) -> "SavedFilterData":
    """Decode the member's acquisition indices and validate its binary well array."""
    match = _FILTER_MEMBER.fullmatch(member)
    if match is None:
      raise ABI7500ProtocolError(f"Unsupported filter member name: {member}")
    stage, cycle, point, filter_index = (int(value) for value in match.groups())
    if min(stage, cycle, point) < 1:
      raise ABI7500ProtocolError("Saved acquisition indices must be positive")
    return cls(stage, cycle, point, filter_index, member, data)


def read_eds_filter_data(path: Union[str, Path]) -> Tuple[SavedFilterData, ...]:
  """Read available filter arrays from a 96-well 7500 experiment without extracting the archive.

  Results are sorted by stage, cycle, collection point, and filter. An experiment stopped before
  collection can return an empty tuple. Missing cycles or filters are not synthesized. Other ZIP
  members, including experiment metadata and calibration snapshots, are left untouched.

  Raises:
    ABI7500ProtocolError: A filter member has an unsupported name, incorrect length, or duplicate
      acquisition indices. This function accepts only the recovered 96-well array format.
    zipfile.BadZipFile: The file is not a valid ZIP archive or a member fails its CRC check.
  """
  results = {}
  with zipfile.ZipFile(path) as archive:
    for member in archive.infolist():
      if not member.filename.startswith("apldbio/sds/images/") or not member.filename.endswith(
        ".tiff"
      ):
        continue
      if member.file_size != _WELL_ARRAY.size:
        raise ABI7500ProtocolError(f"Expected 768 bytes in {member.filename}")
      data = SavedFilterData.decode(member.filename, archive.read(member))
      key = (data.stage, data.cycle, data.collection_point, data.filter)
      if key in results:
        raise ABI7500ProtocolError(f"Duplicate saved filter indices: {key}")
      results[key] = data
  return tuple(results[key] for key in sorted(results))
