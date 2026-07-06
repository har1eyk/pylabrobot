import unittest

from pylabrobot.resources import Kimble_tube_4mL_Rb
from pylabrobot.resources.kimble import Kimble_tube_4mL_Rb as package_factory
from pylabrobot.resources.tube import TubeBottomType


class KimbleTubeTests(unittest.TestCase):
  def test_kimble_tube_4ml_rb(self):
    self.assertIs(Kimble_tube_4mL_Rb, package_factory)

    tube = Kimble_tube_4mL_Rb(name="kimble_tube")
    self.assertEqual(tube.name, "kimble_tube")
    self.assertEqual(tube.model, "Kimble_tube_4mL_Rb")
    self.assertEqual(tube.get_size_x(), 10)
    self.assertEqual(tube.get_size_y(), 10)
    self.assertEqual(tube.get_size_z(), 75)
    self.assertEqual(tube.max_volume, 4_000)
    self.assertEqual(tube.bottom_type, TubeBottomType.U)
    self.assertEqual(tube.material_z_thickness, 1)
    self.assertTrue(tube.supports_compute_height_volume_functions())

    self.assertAlmostEqual(tube.compute_height_from_volume(1000), 18.6717093)
    self.assertAlmostEqual(tube.compute_volume_from_height(35.0925523), 2000)
    self.assertAlmostEqual(tube.compute_height_from_volume(0), 2.2508663)
    self.assertAlmostEqual(
      tube.compute_volume_from_height(0),
      (0 - 2.2508663) / 0.016420843,
    )
