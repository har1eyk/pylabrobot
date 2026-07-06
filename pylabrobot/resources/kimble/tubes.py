"""Resource definitions for Kimble tubes."""

from pylabrobot.resources.tube import Tube, TubeBottomType


_KIMBLE_TUBE_4ML_RB_HEIGHT_PER_UL = 0.016420843
_KIMBLE_TUBE_4ML_RB_HEIGHT_INTERCEPT = 2.2508663


def _compute_height_from_volume_Kimble_tube_4mL_Rb(volume: float) -> float:
  """Compute liquid height in mm from volume in uL using a linear fit."""
  return (
    _KIMBLE_TUBE_4ML_RB_HEIGHT_PER_UL * volume
    + _KIMBLE_TUBE_4ML_RB_HEIGHT_INTERCEPT
  )


def _compute_volume_from_height_Kimble_tube_4mL_Rb(height: float) -> float:
  """Compute volume in uL from liquid height in mm using a linear fit."""
  return (
    (height - _KIMBLE_TUBE_4ML_RB_HEIGHT_INTERCEPT)
    / _KIMBLE_TUBE_4ML_RB_HEIGHT_PER_UL
  )


def Kimble_tube_4mL_Rb(name: str) -> Tube:
  """KIMBLE disposable rimless culture tube, catalog no. 73500-1075.

  - Manufacturer: Kimble / DWK Life Sciences
  - Material: 51 expansion borosilicate glass (USP Type I, ASTM E438 Type I Class B)
  - Outside diameter: 10 mm
  - Length: 75 mm
  - Overflow capacity: 4 mL
  - Bottom: round
  - Rim: rimless / plain neck
  - Sterility: non-sterile
  - Marking spot: none
  - Packaging: 1000/case in shrink-wrapped trays
  - ASTM specification: ASTM E890
  - Manufacturer URL: https://www.dwk.com/kimble-plain-disposable-borosilicate-glass-tube-10-x-75mm-4-ml-73500-1075
  - Distributor URL: https://www.sigmaaldrich.com/US/en/product/aldrich/dwk735001075
  """
  diameter = 10  # from spec
  return Tube(
    name=name,
    size_x=diameter,
    size_y=diameter,
    size_z=75,  # from spec
    model=Kimble_tube_4mL_Rb.__name__,
    max_volume=4_000,  # from spec; units: uL
    material_z_thickness=1,  # measured
    bottom_type=TubeBottomType.U,
    compute_volume_from_height=_compute_volume_from_height_Kimble_tube_4mL_Rb,
    compute_height_from_volume=_compute_height_from_volume_Kimble_tube_4mL_Rb,
  )
