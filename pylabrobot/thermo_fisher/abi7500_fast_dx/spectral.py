"""Offline dye separation and passive-reference normalization of 7500 filter measurements.

Inputs must already have native background/exposure and uniformity processing. Dye components
are signed relative signals, not concentrations. No baseline subtraction, Ct or assay calling
is performed. Calibration provenance does not establish present-day instrument validity.
"""

import hashlib
import math
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Union

from ._spectral_math import spectral_inverse, spectral_residual, spectral_signals
from .calibration import UniformityCorrectedData, _coefficient, _float32, _parse_ini
from .fluorescence import CameraFilterResult


def _positive_index(value: int, maximum: int | None = None) -> None:
  """Validate a one-based integer index without accepting bool."""
  if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
    raise ValueError("Expected a one-based integer index within the supported range")


def _filters(values: tuple[int, ...]) -> None:
  """Require explicitly ordered, unique saved-filter indices."""
  if type(values) is not tuple or not values or tuple(sorted(set(values))) != values:
    raise ValueError("Enabled saved filters must be an increasing immutable tuple")
  for value in values:
    _positive_index(value, 5)


@dataclass(frozen=True)
class DyeCalibration:
  """One explicitly selected dye from a saved five-filter 7500-family calibration.

  Original bytes, saved pass status, timestamps and instrument identifiers are retained.
  Wildcard identifiers are preserved, never treated as proof of an instrument match.
  Spectra are in saved-filter order 1–5 and row-major wells 1–96. Physical filters run
  in the opposite order. ``review_wells`` carries known repairs or unresolved concerns
  from the accompanying calibration report; the INI alone does not record these.
  ``allow_failed`` permits diagnostic replay of failed historical snapshots; their
  measurements always retain a blocking calibration-failed flag.
  """

  source_bytes: bytes
  dye: str
  review_wells: tuple[int, ...] = ()
  allow_failed: bool = False

  @cached_property
  def _fields(self) -> dict[str, dict[str, tuple[str, ...]]]:
    """Parse the immutable source once."""
    return _parse_ini(self.source_bytes)

  def _scalar(self, section: str, key: str) -> str:
    """Read one required metadata scalar."""
    value = self._fields.get(section, {}).get(key)
    if value is None or len(value) != 1:
      raise ValueError(f"Missing dye calibration field [{section}] {key}")
    return value[0]

  @cached_property
  def sha256(self) -> str:
    """Hash the original file, including all metadata and other saved dyes."""
    return hashlib.sha256(self.source_bytes).hexdigest()

  @property
  def instrument_id(self) -> str:
    """Return the recorded instrument ID verbatim."""
    return self._scalar("instrument", "id")

  @property
  def instrument_type(self) -> str:
    """Return the saved 7500-family type without treating models as interchangeable."""
    return self._scalar("instrument", "type")

  @property
  def scanner_id(self) -> str:
    """Return the recorded scanner ID verbatim."""
    return self._scalar("scanner", "id")

  @property
  def calibration_time(self) -> int:
    """Return the selected dye's UTC timestamp in milliseconds since the Unix epoch."""
    return int(self._scalar(self.dye, "calibration-time"))

  @property
  def expiration_time(self) -> int:
    """Return the saved expiry field; zero does not certify indefinite validity."""
    return int(self._scalar(self.dye, "expiration-time"))

  @property
  def temperature(self) -> float:
    """Return the recorded calibration temperature in degrees Celsius."""
    return float(self._scalar(self.dye, "temperature"))

  @property
  def passed(self) -> bool:
    """Return the saved pass flag, not a statement about current calibration validity."""
    return self._scalar(self.dye, "result") == "true"

  @cached_property
  def coefficients(self) -> tuple[tuple[float, ...], ...]:
    """Read nonnegative binary32 spectra for all five saved filters and 96 wells."""
    values = []
    for index in range(1, 6):
      rows = self._fields[self.dye].get(str(index), ())
      if len(rows) != 8 or any(len(row.split(",")) != 12 for row in rows):
        raise ValueError("Each dye filter requires eight rows of twelve coefficients")
      channel = []
      for row in rows:
        for text in row.split(","):
          # _coefficient accepts positive values and rounds decimals directly to binary32.
          try:
            zero = Decimal(text).is_zero()
          except InvalidOperation as exc:
            raise ValueError("Invalid dye coefficient") from exc
          if zero:
            channel.append(0.0)
          else:
            channel.append(_coefficient(text))
      values.append(tuple(channel))
    return tuple(values)

  def __post_init__(self) -> None:
    """Reject failed, ambiguous or incompatible selected calibration data."""
    if not isinstance(self.source_bytes, bytes) or not isinstance(self.dye, str) or not self.dye:
      raise ValueError("Supply immutable calibration bytes and an exact dye name")
    required = {
      ("head", "version"): "1.0.0",
      ("scanner", "filters"): "1,2,3,4,5",
    }
    for (section, key), expected in required.items():
      if self._scalar(section, key) != expected:
        raise ValueError(f"Unsupported or unsuccessful dye calibration: [{section}] {key}")
    if self.instrument_type not in ("sds7500fast", "sds7500"):
      raise ValueError("Unsupported dye calibration instrument type")
    if type(self.allow_failed) is not bool or self._scalar(self.dye, "result") not in (
      "true",
      "false",
    ):
      raise ValueError("Invalid dye calibration pass status")
    if not self.passed and not self.allow_failed:
      raise ValueError("Failed dye calibration; allow_failed is for diagnostic replay only")
    names = self._scalar("puredyes", "dyes").split(",")
    if len(names) != len(set(names)) or self.dye not in names:
      raise ValueError("Dye must appear exactly once in the calibration dye list")
    if self.calibration_time < 0 or self.expiration_time < 0 or not math.isfinite(self.temperature):
      raise ValueError("Invalid dye calibration metadata")
    if not self.instrument_id or not self.scanner_id:
      raise ValueError("Missing instrument or scanner provenance")
    self.coefficients
    if type(self.review_wells) is not tuple or len(set(self.review_wells)) != len(
      self.review_wells
    ):
      raise ValueError("Review wells must be a unique immutable tuple")
    for well in self.review_wells:
      _positive_index(well, 96)

  def spectrum(self, well: int) -> tuple[float, ...]:
    """Normalize one well's spectrum by its maximum across all five filters."""
    _positive_index(well, 96)
    values = tuple(channel[well - 1] for channel in self.coefficients)
    peak = max(values)
    if peak == 0:
      raise ValueError("Empty pure-dye spectrum")
    return tuple(_float32(value / peak) for value in values)

  @classmethod
  def from_file(
    cls,
    path: Union[str, Path],
    dye: str,
    *,
    review_wells: tuple[int, ...] = (),
    allow_failed: bool = False,
  ) -> "DyeCalibration":
    """Load a selected dye without writing files or accessing the instrument."""
    return cls(Path(path).read_bytes(), dye, review_wells, allow_failed)


def read_eds_dye_calibration(
  path: Union[str, Path],
  dye: str,
  *,
  review_wells: tuple[int, ...] = (),
  allow_failed: bool = False,
) -> DyeCalibration:
  """Read the one unambiguous pure-dye snapshot embedded in an EDS archive."""
  with zipfile.ZipFile(path) as archive:
    matches = [
      info
      for info in archive.infolist()
      if info.filename.replace("\\", "/").lower() == "apldbio/sds/calibrations/puredye.ini"
    ]
    if len(matches) != 1 or matches[0].file_size > 1024 * 1024:
      raise ValueError("Expected one bounded pure-dye calibration member")
    return DyeCalibration(archive.read(matches[0]), dye, review_wells, allow_failed)


@dataclass(frozen=True)
class FilterSignal:
  """One saved-filter value, with explicit missing-data and saturation status.

  ``value`` has already undergone background/exposure and uniformity processing.
  None means missing, never zero. ``saturated=None`` means saturation was not recorded.
  """

  filter: int
  value: float | None
  saturated: bool | None

  def __post_init__(self) -> None:
    """Reject invalid filter indices and nonfinite values."""
    _positive_index(self.filter, 5)
    if self.value is not None:
      if isinstance(self.value, bool) or not math.isfinite(self.value):
        raise ValueError("Filter values must be finite numbers or None")
      try:
        _float32(self.value)
      except OverflowError as exc:
        raise ValueError("Filter value exceeds the binary32 range") from exc
    if self.saturated is not None and type(self.saturated) is not bool:
      raise ValueError("Saturation must be True, False or unknown (None)")


@dataclass(frozen=True)
class WellFilterSignals:
  """A single well at one collection point, retaining explicit input provenance.

  Well and stage are one-based. Cycle and collection point preserve source indices;
  zero is allowed for older EDS coordinates and is never silently renumbered.
  """

  well: int
  stage: int
  cycle: int
  collection_point: int
  signals: tuple[FilterSignal, ...]
  source_id: str
  review_reasons: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    """Reject duplicate filters, missing provenance and mutable inputs."""
    _positive_index(self.well, 96)
    _positive_index(self.stage)
    for value in (self.cycle, self.collection_point):
      if type(value) is not int or value < 0:
        raise ValueError("Cycle and collection point must be nonnegative source indices")
    if type(self.signals) is not tuple or any(
      not isinstance(s, FilterSignal) for s in self.signals
    ):
      raise ValueError("Signals must be an immutable tuple of FilterSignal objects")
    if len({s.filter for s in self.signals}) != len(self.signals):
      raise ValueError("Duplicate saved-filter signal")
    if not isinstance(self.source_id, str) or not self.source_id.strip():
      raise ValueError("Supply an input artifact or acquisition identifier")
    if type(self.review_reasons) is not tuple or any(
      not isinstance(reason, str) or not reason.strip() for reason in self.review_reasons
    ):
      raise ValueError("Review reasons must be immutable nonempty strings")


@dataclass(frozen=True)
class DyeMeasurement:
  """Separated signed dye components and a guarded reporter/reference ratio.

  ``unrestricted_rn`` retains the finite vendor-style ratio for numerical comparisons,
  including negative or weak references. ``rn`` is None when quality flags require review.
  A finite ratio is not evidence of valid calibration or an analytically valid PCR assay.
  """

  source: WellFilterSignals
  calibrations: tuple[DyeCalibration, ...]
  enabled_filters: tuple[int, ...]
  reporter: str
  passive_reference: str | None
  minimum_reference: float
  max_condition: float
  components: tuple[tuple[str, float], ...]
  condition_number: float | None
  residual_standard_error: float | None
  unrestricted_rn: float | None
  rn: float | None
  flags: tuple[str, ...]

  def report(self) -> dict[str, Any]:
    """Return JSON-compatible values and provenance without copying full calibration files."""
    return {
      "well": self.source.well,
      "stage": self.source.stage,
      "cycle": self.source.cycle,
      "collection_point": self.source.collection_point,
      "source_id": self.source.source_id,
      "review_reasons": self.source.review_reasons,
      "input_filters": [
        {"filter": s.filter, "value": s.value, "saturated": s.saturated}
        for s in self.source.signals
      ],
      "enabled_filters": self.enabled_filters,
      "calibrations": [
        {
          "dye": c.dye,
          "sha256": c.sha256,
          "instrument_id": c.instrument_id,
          "instrument_type": c.instrument_type,
          "scanner_id": c.scanner_id,
          "calibration_time": c.calibration_time,
          "expiration_time": c.expiration_time,
          "temperature": c.temperature,
          "review_wells": c.review_wells,
          "saved_passed": c.passed,
        }
        for c in self.calibrations
      ],
      "components": dict(self.components),
      "reporter": self.reporter,
      "passive_reference": self.passive_reference,
      "minimum_reference": self.minimum_reference,
      "max_condition": self.max_condition,
      "condition_number": self.condition_number,
      "residual_standard_error": self.residual_standard_error,
      "unrestricted_rn": self.unrestricted_rn,
      "rn": self.rn,
      "flags": self.flags,
    }


@lru_cache(maxsize=1024)
def _inverse(
  matrix: tuple[tuple[float, ...], ...], max_condition: float
) -> tuple[tuple[tuple[float, ...], ...], float]:
  """Reuse only an identical spectral matrix and condition-number policy."""
  return spectral_inverse(matrix, max_condition)


def unmix_well(
  source: WellFilterSignals,
  calibrations: tuple[DyeCalibration, ...],
  *,
  reporter: str,
  passive_reference: str | None = "ROX",
  enabled_filters: tuple[int, ...] = (1, 2, 3, 4, 5),
  minimum_reference: float = 10.0,
  max_condition: float = 10000.0,
) -> DyeMeasurement:
  """Separate one well's dyes and optionally normalize its reporter to a passive reference.

  Supply every fluorescent component, including fluorescent quenchers when applicable.
  Dye names are exact and explicit: FAM, SYBR, fluorescein and EvaGreen are not aliases.
  Missing requested filters are flagged, never silently dropped or filled with zeros.
  A subset is allowed only by explicitly selecting ``enabled_filters``. Singular or
  ill-conditioned calibrations return an invalid measurement, not a regularized fit.

  The 10-unit reference floor is the recovered vendor default. The condition limit is
  a PLR numerical safeguard, not a vendor calibration acceptance limit. Unknown
  saturation, negative components, weak references and explicit review concerns suppress
  ``rn`` while preserving diagnostic components and any finite ``unrestricted_rn``.
  """
  if not isinstance(source, WellFilterSignals):
    raise TypeError("Supply a WellFilterSignals measurement")
  _filters(enabled_filters)
  if (
    type(calibrations) is not tuple
    or not calibrations
    or any(not isinstance(c, DyeCalibration) for c in calibrations)
  ):
    raise ValueError("Supply an immutable tuple of dye calibrations")
  dyes = tuple(c.dye for c in calibrations)
  if len(set(dyes)) != len(dyes) or len(dyes) > len(enabled_filters):
    raise ValueError("Dyes must be unique and cannot outnumber enabled filters")
  if reporter not in dyes or (passive_reference is not None and passive_reference not in dyes):
    raise ValueError("Reporter and passive reference must have explicit calibrations")
  if reporter == passive_reference:
    raise ValueError("Reporter and passive reference must be different dyes")
  if len({c.instrument_type for c in calibrations}) > 1:
    raise ValueError("Dye calibration instrument types conflict")
  for value, floor in ((minimum_reference, 0), (max_condition, 1)):
    if isinstance(value, bool) or not math.isfinite(value) or value < floor:
      raise ValueError("Invalid reference floor or spectral condition limit")
  for attribute in ("instrument", "scanner"):
    identities = {
      c.instrument_id if attribute == "instrument" else c.scanner_id for c in calibrations
    } - {"null", "*"}
    if len(identities) > 1:
      raise ValueError("Dye calibration instrument/scanner identities conflict")
  flags = []
  if any(not c.passed for c in calibrations):
    flags.append("calibration_failed")
  if source.review_reasons or any(source.well in c.review_wells for c in calibrations):
    flags.append("review_required")
  if any(c.instrument_id in ("null", "*") or c.scanner_id in ("null", "*") for c in calibrations):
    flags.append("calibration_identity_unspecified")
  by_filter = {signal.filter: signal for signal in source.signals}
  selected = [by_filter.get(index) for index in enabled_filters]
  components: tuple[tuple[str, float], ...] = ()
  condition = residual = ratio = None
  if any(s is None or s.value is None for s in selected):
    flags.append("missing_filter")
  else:
    if any(s is not None and s.saturated is True for s in selected):
      flags.append("saturated")
    if any(s is not None and s.saturated is None for s in selected):
      flags.append("saturation_unknown")
    measured = tuple(_float32(s.value) for s in selected if s is not None and s.value is not None)
    try:
      spectra = tuple(c.spectrum(source.well) for c in calibrations)
      matrix = tuple(tuple(row[index - 1] for row in spectra) for index in enabled_filters)
      inverse, condition = _inverse(matrix, max_condition)
      values = spectral_signals(inverse, measured)
      residual = spectral_residual(matrix, measured, values)
      if any(not math.isfinite(value) for value in values) or (
        residual is not None and not math.isfinite(residual)
      ):
        raise ValueError("Nonfinite spectral result")
      components = tuple(zip(dyes, values))
    except (ValueError, OverflowError):
      flags.append("invalid_spectral_calibration")
    if components:
      signals = dict(components)
      if any(value < 0 for value in signals.values()):
        flags.append("negative_component")
      if passive_reference is None:
        flags.append("no_passive_reference")
      else:
        reference = signals[passive_reference]
        if reference <= 0:
          flags.append("nonpositive_reference")
        elif reference < minimum_reference:
          flags.append("low_reference")
        if reference:
          try:
            ratio = _float32(signals[reporter] / reference)
          except OverflowError:
            flags.append("nonfinite_ratio")
  blocking = set(flags) - {"calibration_identity_unspecified"}
  return DyeMeasurement(
    source,
    calibrations,
    enabled_filters,
    reporter,
    passive_reference,
    minimum_reference,
    max_condition,
    components,
    condition,
    residual,
    ratio,
    ratio if not blocking else None,
    tuple(flags),
  )


def unmix_collection(
  results: tuple[CameraFilterResult, ...] | tuple[UniformityCorrectedData, ...],
  calibrations: tuple[DyeCalibration, ...],
  *,
  reporter: str,
  passive_reference: str | None = "ROX",
  enabled_filters: tuple[int, ...] = (1, 2, 3, 4, 5),
  minimum_reference: float = 10.0,
  max_condition: float = 10000.0,
  review_wells: tuple[int, ...] = (),
) -> tuple[DyeMeasurement, ...]:
  """Process 96 wells from one saved or camera-derived collection point, entirely offline.

  The existing extraction objects retain originals; their source and calibration hashes form
  each output's input identifier. All provided results must share acquisition coordinates and
  uniformity calibration. Camera results must also share native background calibration.
  Saved EDS arrays have unknown saturation; their unrestricted ratios remain available for
  vendor comparison, but guarded ``rn`` values are withheld.
  """
  if type(results) is not tuple or not results:
    raise ValueError("Supply immutable results from one collection point")
  if type(review_wells) is not tuple or len(set(review_wells)) != len(review_wells):
    raise ValueError("Review wells must be a unique immutable tuple")
  for well in review_wells:
    _positive_index(well, 96)
  if not (
    all(isinstance(r, CameraFilterResult) for r in results)
    or all(isinstance(r, UniformityCorrectedData) for r in results)
  ):
    raise TypeError("Do not mix saved arrays and camera results")
  coordinates, uniformities, backgrounds = set(), set(), set()
  values: dict[int, tuple[FilterSignal, ...]] = {}
  provenance: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []
  for result in results:
    if isinstance(result, CameraFilterResult):
      coordinates.add((result.stage, result.cycle, result.collection_point))
      index = result.filter
      uniformities.add(result.uniformity.sha256)
      backgrounds.add(result.background.sha256)
      measurements = result.measurements
      data = tuple(
        FilterSignal(index, m.corrected_value, m.selected_saturated) for m in measurements
      )
      original = tuple(
        f"{hashlib.sha256(f.source.wire_data).hexdigest()}@{f.exposure.milliseconds}ms"
        for f in result.frames
      )
      provenance.append((index, original, tuple(f.mask.sha256 for f in result.frames)))
    else:
      coordinates.add((result.source.stage, result.source.cycle, result.source.collection_point))
      index = result.source.filter
      uniformities.add(result.calibration.sha256)
      data = tuple(FilterSignal(index, m.corrected_value, None) for m in result.measurements)
      provenance.append((index, (hashlib.sha256(result.source.source_bytes).hexdigest(),), ()))
    if index in values:
      raise ValueError("Duplicate saved-filter result")
    values[index] = data
  if len(coordinates) != 1 or len(uniformities) != 1 or len(backgrounds) > 1:
    raise ValueError("Collection point or processing calibrations differ")
  stage, cycle, point = next(iter(coordinates))
  source_id = (
    "sha256:"
    + hashlib.sha256(
      repr((sorted(provenance), sorted(uniformities), sorted(backgrounds))).encode()
    ).hexdigest()
  )
  return tuple(
    unmix_well(
      WellFilterSignals(
        well,
        stage,
        cycle,
        point,
        tuple(values[index][well - 1] for index in sorted(values)),
        source_id,
        ("Caller requested well review",) if well in review_wells else (),
      ),
      calibrations,
      reporter=reporter,
      passive_reference=passive_reference,
      enabled_filters=enabled_filters,
      minimum_reference=minimum_reference,
      max_condition=max_condition,
    )
    for well in range(1, 97)
  )
