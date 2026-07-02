"""Resource definitions for Kimble tubes."""

from pylabrobot.resources.tube import Tube, TubeBottomType


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
    bottom_type=TubeBottomType.U,
  )
