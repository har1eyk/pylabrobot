"""Public models used by the FilterMax F5 driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Literal, Mapping, Optional, Tuple

FilterKind = Literal["excitation", "emission"]
FilterTechnique = Literal[
  "absorbance",
  "fluorescence",
  "fluorescence_polarization",
  "time_resolved_fluorescence",
  "luminescence",
]
ShakePattern = Literal["linear", "orbital"]
ShakeSpeed = Literal["low", "medium", "high"]
WellScanPattern = Literal["horizontal", "fill"]
PlateOrientation = Literal["landscape", "portrait"]


@dataclass(frozen=True)
class InstrumentInfo:
  model: str
  firmware: str
  device_number: int
  serial_number: str
  excitation_slide_id: int
  emission_slide_id: int
  device_code: int
  plate_control: bool
  pic_firmware: str
  cpld_version: int
  raw: str = field(repr=False)


@dataclass(frozen=True)
class FilterMaxStatus:
  ready: bool
  text: str
  state_bits: Tuple[int, ...]


@dataclass(frozen=True)
class InstalledFilterSlides:
  excitation_slide_id: int
  emission_slide_id: int


@dataclass(frozen=True)
class FilterDefinition:
  kind: FilterKind
  slide_id: int
  slide_name: str
  position: int
  wavelength: int
  bandwidth: int
  apply_to: int
  techniques: FrozenSet[FilterTechnique]
  molecular_devices_number: Optional[str] = None


@dataclass(frozen=True)
class FilterSelection:
  kind: FilterKind
  slide_id: int
  position: int
  wavelength: int
  bandwidth: int


@dataclass(frozen=True)
class KineticTiming:
  interval: float
  reads: int

  def __post_init__(self) -> None:
    if self.interval <= 0:
      raise ValueError("interval must be positive")
    if self.reads < 1:
      raise ValueError("reads must be at least 1")


@dataclass(frozen=True)
class ShakingSettings:
  pattern: ShakePattern
  speed: ShakeSpeed
  duration: int
  between_reads: bool = False

  def __post_init__(self) -> None:
    if self.duration < 1:
      raise ValueError("duration must be at least 1")


@dataclass(frozen=True)
class WellScanSettings:
  pattern: WellScanPattern
  density: int
  point_spacing: float = 0.23

  def __post_init__(self) -> None:
    if self.density < 1:
      raise ValueError("density must be at least 1")
    if self.point_spacing <= 0:
      raise ValueError("point_spacing must be positive")

  @property
  def grid_shape(self) -> Tuple[int, int]:
    return (self.density, 1) if self.pattern == "horizontal" else (self.density, self.density)


@dataclass(frozen=True)
class PlateGeometry:
  """FilterMax plate geometry in public PLR units (millimetres)."""

  rows: int
  columns: int
  length: float
  width: float
  height: float
  bottom_row_offset: float
  left_column_offset: float
  top_row_offset: float
  column_spacing: float
  row_spacing: float
  well_size_x: float
  well_size_y: float
  absorbance_z: float
  name: str = "Custom plate"
  orientation: PlateOrientation = "landscape"

  def __post_init__(self) -> None:
    if self.orientation not in ("landscape", "portrait"):
      raise ValueError(f"Unsupported FilterMax plate orientation {self.orientation!r}")

  @property
  def well_depth(self) -> float:
    """Return the well depth stored under the legacy ``bottom_row_offset`` name."""

    return self.bottom_row_offset

  @classmethod
  def costar_96_clear_landscape(cls) -> "PlateGeometry":
    """Return the live-optimized geometry captured from the validation plate."""

    return cls(
      rows=8,
      columns=12,
      length=127.70,
      width=85.70,
      height=14.27,
      bottom_row_offset=10.69,
      left_column_offset=14.05,
      top_row_offset=11.18,
      column_spacing=9.02,
      row_spacing=9.00,
      well_size_x=6.40,
      well_size_y=6.40,
      absorbance_z=10.27,
      name="96 Well Costar clear [Landscape]",
    )

  @classmethod
  def costar_96_clear_portrait(cls) -> "PlateGeometry":
    """Return the Costar 96-well geometry with portrait scan orientation."""

    return cls(
      rows=8,
      columns=12,
      length=127.70,
      width=85.70,
      height=14.27,
      bottom_row_offset=10.69,
      left_column_offset=14.05,
      top_row_offset=11.18,
      column_spacing=9.02,
      row_spacing=9.00,
      well_size_x=6.40,
      well_size_y=6.40,
      absorbance_z=10.27,
      name="96 Well Costar clear [Portrait]",
      orientation="portrait",
    )


@dataclass(frozen=True)
class WellScanPoint:
  x: float
  y: float
  value: float


PlateData = List[List[Optional[float]]]


@dataclass(frozen=True)
class AbsorbanceResult:
  data: PlateData
  wavelengths: Tuple[int, ...]
  reference_subtracted: bool
  temperature: Optional[float]
  timestamp: float
  elapsed_time: float = 0.0
  scan_points: Mapping[str, Tuple[WellScanPoint, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class LuminescenceResult:
  data: PlateData
  channel: int
  temperature: Optional[float]
  timestamp: float
  elapsed_time: float = 0.0


@dataclass(frozen=True)
class FluorescenceResult:
  data: PlateData
  excitation_wavelength: int
  emission_wavelength: int
  temperature: Optional[float]
  timestamp: float
  elapsed_time: float = 0.0


@dataclass(frozen=True)
class TimeResolvedFluorescenceResult(FluorescenceResult):
  delay: float = 0.0
  integration: float = 0.0


@dataclass(frozen=True)
class FluorescencePolarizationResult:
  parallel_data: PlateData
  perpendicular_data: PlateData
  excitation_wavelength: int
  emission_wavelength: int
  temperature: Optional[float]
  timestamp: float
  elapsed_time: float = 0.0


def empty_plate_data(rows: int, columns: int) -> PlateData:
  return [[None for _ in range(columns)] for _ in range(rows)]
