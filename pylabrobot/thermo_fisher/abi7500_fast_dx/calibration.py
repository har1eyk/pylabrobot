"""Vendor uniformity correction of saved filter arrays, without assay or Ct interpretation.

The input is the vendor's extracted well array, not a camera frame. Native pixel extraction,
exposure splicing and dark/ring correction must not be replaced by this multiplication.
"""

import hashlib
import math
import struct
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Union

from .results import SavedFilterData


def _float32(value: float) -> float:
  """Round once to the vendor Java float representation."""
  return float(struct.unpack(">f", struct.pack(">f", value))[0])


def _integer_float32(value: int) -> float:
  """Convert a signed integer directly to binary32 without double-rounding through binary64."""
  magnitude = abs(value)
  shift = max(0, magnitude.bit_length() - 24)
  if shift:
    significand, remainder = divmod(magnitude, 1 << shift)
    halfway = 1 << (shift - 1)
    if remainder > halfway or (remainder == halfway and significand % 2):
      significand += 1
    magnitude = significand << shift
  return _float32(float(-magnitude if value < 0 else magnitude))


def _coefficient(text: str) -> float:
  """Parse a positive decimal directly to binary32, including values near rounding ties."""
  try:
    number = Decimal(text)
  except InvalidOperation as exc:
    raise ValueError("Invalid decimal uniformity coefficient") from exc
  if not number.is_finite() or number <= 0 or not -46 <= number.adjusted() <= 38:
    raise ValueError("Uniformity coefficient is outside the positive binary32 range")
  numerator, denominator = number.as_integer_ratio()
  exponent = numerator.bit_length() - denominator.bit_length()
  if exponent >= 0:
    below_power = numerator < denominator << exponent
  else:
    below_power = numerator << -exponent < denominator
  if below_power:
    exponent -= 1
  quantum = max(exponent - 23, -149)
  if quantum < 0:
    numerator <<= -quantum
  else:
    denominator <<= quantum
  significand, remainder = divmod(numerator, denominator)
  if 2 * remainder > denominator or (2 * remainder == denominator and significand % 2):
    significand += 1
  return _float32(math.ldexp(float(significand), quantum))


def _parse_ini(source: bytes) -> dict[str, dict[str, tuple[str, ...]]]:
  """Read the vendor's scalar and <<eof multiline fields, rejecting ambiguous duplicates."""
  if not isinstance(source, bytes) or len(source) > 1024 * 1024:
    raise ValueError("Expected immutable calibration bytes, at most 1 MiB")
  sections: dict[str, dict[str, tuple[str, ...]]] = {}
  section: dict[str, tuple[str, ...]] = {}
  lines = iter(source.decode("utf-8-sig").splitlines())
  for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#"):
      continue
    if line.startswith("[") and line.endswith("]"):
      name = line[1:-1]
      if name in sections:
        raise ValueError(f"Duplicate calibration section: {name}")
      section = {}
      sections[name] = section
    elif "=" in line and sections:
      key, value = (part.strip() for part in line.split("=", 1))
      if key in section:
        raise ValueError(f"Duplicate calibration field: {key}")
      if value == "<<eof":
        rows = []
        for row in lines:
          if row.strip() == "eof":
            break
          rows.append(row.strip())
        else:
          raise ValueError("Unterminated calibration matrix")
        section[key] = tuple(rows)
      else:
        section[key] = (value,)
    else:
      raise ValueError("Malformed calibration field")
  return sections


@dataclass(frozen=True)
class UniformityCalibration:
  """An immutable 7500 Fast uniformity file with its original provenance.

  This validates format and the saved pass flag, not present-day calibration validity or identity.
  The reference files contain instrument id 'null' and scanner id '*'; these do not bind them to
  a camera. Select the calibration saved with the experiment when comparing vendor analysis.
  No recalibration or file writes are performed. Full source bytes should remain outside Git.
  """

  source_bytes: bytes

  def _scalar(self, section: str, key: str) -> str:
    """Require one provenance value from the original file."""
    values = _parse_ini(self.source_bytes).get(section, {}).get(key)
    if values is None or len(values) != 1:
      raise ValueError(f"Missing scalar calibration field: [{section}] {key}")
    return values[0]

  @property
  def sha256(self) -> str:
    """Identify the exact calibration bytes, including provenance fields."""
    return hashlib.sha256(self.source_bytes).hexdigest()

  @property
  def timestamp(self) -> int:
    """Return the vendor timestamp in milliseconds since the Unix epoch, in UTC."""
    return int(self._scalar("head", "timestamp"))

  @property
  def instrument_id(self) -> str:
    """Return the saved instrument identifier verbatim, including 'null' when unspecified."""
    return self._scalar("instrument", "id")

  @property
  def scanner_id(self) -> str:
    """Return the saved scanner identifier verbatim; '*' is not a camera identity."""
    return self._scalar("scanner", "id")

  @property
  def coefficients(self) -> tuple[tuple[float, ...], ...]:
    """Return binary32 factors in filter 1–5, then row-major well 1–96 order."""
    fields = _parse_ini(self.source_bytes)["uniformity"]
    return tuple(
      tuple(_coefficient(value) for row in fields[str(index)] for value in row.split(","))
      for index in range(1, 6)
    )

  def __post_init__(self) -> None:
    """Reject unsupported instruments, failed results and malformed coefficient matrices."""
    fields = _parse_ini(self.source_bytes)
    required = {
      ("head", "version"): "1.0.0",
      ("instrument", "type"): "sds7500fast",
      ("scanner", "filters"): "1,2,3,4,5",
      ("block", "type"): "96fast",
      ("block", "rows"): "8",
      ("block", "columns"): "12",
      ("result", "result"): "true",
    }
    for (section, key), expected in required.items():
      if fields.get(section, {}).get(key) != (expected,):
        raise ValueError(f"Unsupported or unsuccessful calibration: [{section}] {key}")
    if self.timestamp < 0:
      raise ValueError("Calibration timestamp must be nonnegative")
    self.instrument_id
    self.scanner_id
    if set(fields.get("uniformity", {})) != {str(i) for i in range(1, 6)}:
      raise ValueError("Uniformity must contain exactly filters 1 through 5")
    for index in range(1, 6):
      rows = fields["uniformity"][str(index)]
      if len(rows) != 8 or any(len(row.split(",")) != 12 for row in rows):
        raise ValueError("Each uniformity filter must contain eight rows of twelve values")
    try:
      coefficients = self.coefficients
    except (OverflowError, ValueError) as exc:
      raise ValueError("Invalid uniformity coefficient") from exc
    if any(not math.isfinite(v) or v <= 0 for row in coefficients for v in row):
      raise ValueError("Uniformity coefficients must be finite and positive")

  @classmethod
  def from_file(cls, path: Union[str, Path]) -> "UniformityCalibration":
    """Load a saved calibration without altering it or accessing the instrument."""
    return cls(Path(path).read_bytes())


@dataclass(frozen=True)
class WellMeasurement:
  """One vendor well's original integer and uniformity-corrected filter value.

  Well numbers are one-based vendor indices; labels use the Java calibration's row-major 8×12
  indexing. This does not independently establish physical orientation of a PLR camera image.
  Corrected values are relative filter signals, not dye quantities, Rn, ΔRn or concentrations.
  """

  well: int
  raw_value: int
  uniformity_coefficient: float
  corrected_value: float

  @property
  def label(self) -> str:
    """Return the vendor row-major plate label A1 through H12."""
    return f"{chr(ord('A') + (self.well - 1) // 12)}{(self.well - 1) % 12 + 1}"


@dataclass(frozen=True)
class UniformityCorrectedData:
  """A saved filter acquisition plus its explicit calibration, preserving all original data."""

  source: SavedFilterData
  calibration: UniformityCalibration

  def __post_init__(self) -> None:
    """Keep diagnostic camera pixel statistics separate from vendor-produced well arrays."""
    if not isinstance(self.source, SavedFilterData):
      raise TypeError("Uniformity correction requires saved vendor filter data")

  @property
  def measurements(self) -> tuple[WellMeasurement, ...]:
    """Apply the vendor's int64→float32 conversion and float32 multiplication exactly once.

    No background subtraction, normalization, spectral unmixing or clipping is implied. Retaining
    the original source prevents cumulative correction when this property is accessed repeatedly.
    """
    factors = self.calibration.coefficients[self.source.filter - 1]
    return tuple(
      WellMeasurement(index + 1, value, factor, _float32(_integer_float32(value) * factor))
      for index, (value, factor) in enumerate(zip(self.source.well_values, factors))
    )


def apply_uniformity(
  data: SavedFilterData, calibration: UniformityCalibration
) -> UniformityCorrectedData:
  """Correct one vendor well array with an explicitly selected saved calibration.

  New camera frames require validated native well extraction before they can use this path.
  Calibration age, experiment association and instrument association remain the caller's
  responsibility; the file's pass flag alone does not establish any of these.
  """
  return UniformityCorrectedData(data, calibration)


def read_eds_uniformity(path: Union[str, Path]) -> UniformityCalibration:
  """Read the uniformity snapshot stored in an EDS archive, without extracting any members.

  Missing or duplicate calibration snapshots are errors; no current-PC calibration is substituted.
  The snapshot establishes which coefficients the experiment saved, not their present validity.
  """
  with zipfile.ZipFile(path) as archive:
    matches = [
      member
      for member in archive.infolist()
      if member.filename.replace("\\", "/").lower() == "apldbio/sds/calibrations/uniformity.ini"
    ]
    if len(matches) != 1 or matches[0].file_size > 1024 * 1024:
      raise ValueError("Expected exactly one bounded EDS uniformity calibration")
    return UniformityCalibration(archive.read(matches[0]))
