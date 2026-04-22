from pylabrobot.resources.plate import Plate
from pylabrobot.resources.utils import create_ordered_items_2d
from pylabrobot.resources.well import (
  CrossSectionType,
  Well,
  WellBottomType,
)


def MTCBio_24_wellplate_10mL_Vb(name: str) -> Plate:
  """MTC Bio Cat. No. D3024-01."""
  # technical drawing:
  # https://www.usplastic.com/catalog/files/drawings/88796Drawing.pdf

  # product info:
  # https://mtcbiotech.com/product/24-well-plates/
  # https://www.usplastic.com/catalog/item.aspx?itemid=153389

  return Plate(
    name=name,
    size_x=127.50,  # from spec
    size_y=85.50,  # from spec
    size_z=44.00,  # from spec
    lid=None,
    model=MTCBio_24_wellplate_10mL_Vb.__name__,
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=6,  # from spec
      num_items_y=4,  # from spec
      dx=18.75 - 17.10 / 2,  # from spec
      dy=15.75 - 17.10 / 2,  # from spec
      dz=44.00 - 41.90 - 1.20,  # from spec
      item_dx=18.00,  # from spec
      item_dy=18.00,  # from spec
      size_x=17.10,  # from spec
      size_y=17.10,  # from spec
      size_z=41.90,  # from spec
      material_z_thickness=1.20,  # from spec
      cross_section_type=CrossSectionType.RECTANGLE,
      bottom_type=WellBottomType.V,
      max_volume=10000,  # from spec 10 mL
    ),
  )
