import unittest

from pylabrobot.resources import McKesson_tube_5mL_Rb
from pylabrobot.resources.mckesson import McKesson_tube_5mL_Rb as package_factory
from pylabrobot.resources.tube import TubeBottomType


class McKessonTubeTests(unittest.TestCase):
  def test_mckesson_tube_5ml_rb(self):
    self.assertIs(McKesson_tube_5mL_Rb, package_factory)

    tube = McKesson_tube_5mL_Rb(name="mckesson_tube")
    self.assertEqual(tube.name, "mckesson_tube")
    self.assertEqual(tube.model, "McKesson_tube_5mL_Rb")
    self.assertEqual(tube.get_size_x(), 12)
    self.assertEqual(tube.get_size_y(), 12)
    self.assertEqual(tube.get_size_z(), 75)
    self.assertEqual(tube.max_volume, 5_000)
    self.assertEqual(tube.bottom_type, TubeBottomType.U)
    self.assertTrue(tube.supports_compute_height_volume_functions())

    self.assertAlmostEqual(tube.compute_height_from_volume(0), 3.622768533)
    self.assertAlmostEqual(tube.compute_height_from_volume(2_500), 32.998568533)
    self.assertAlmostEqual(tube.compute_height_from_volume(5_000), 62.374368533)
    self.assertAlmostEqual(tube.compute_volume_from_height(32.998568533), 2_500)
    self.assertAlmostEqual(
      tube.compute_volume_from_height(0),
      (0 - 3.622768533) / 0.01175032,
    )
