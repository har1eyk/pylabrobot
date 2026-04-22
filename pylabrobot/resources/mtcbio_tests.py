import unittest

from pylabrobot.resources import MTCBio_24_wellplate_10mL_Vb
from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources.well import CrossSectionType, WellBottomType


class TestMTCBioResources(unittest.TestCase):
  def test_mtcbio_24_wellplate_10mL_Vb(self):
    plate = MTCBio_24_wellplate_10mL_Vb("plate")

    self.assertEqual(plate.get_size_x(), 127.50)
    self.assertEqual(plate.get_size_y(), 85.50)
    self.assertEqual(plate.get_size_z(), 44.00)
    self.assertEqual(len(plate.get_all_items()), 24)

    a1 = plate.get_well("A1")
    a6 = plate.get_well("A6")
    d1 = plate.get_well("D1")
    d6 = plate.get_well("D6")

    self.assertEqual(a1.get_identifier(), "A1")
    self.assertEqual(a6.get_identifier(), "A6")
    self.assertEqual(d1.get_identifier(), "D1")
    self.assertEqual(d6.get_identifier(), "D6")

    self.assertEqual(a1.location, Coordinate(10.2, 61.2, 0.9))
    self.assertEqual(a6.location, Coordinate(100.2, 61.2, 0.9))
    self.assertEqual(d1.location, Coordinate(10.2, 7.2, 0.9))
    self.assertEqual(d6.location, Coordinate(100.2, 7.2, 0.9))

    self.assertEqual(a1.bottom_type, WellBottomType.V)
    self.assertEqual(a1.cross_section_type, CrossSectionType.RECTANGLE)
    self.assertEqual(a1.max_volume, 10000)
