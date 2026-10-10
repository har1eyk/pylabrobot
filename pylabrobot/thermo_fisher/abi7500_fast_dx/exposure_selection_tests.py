"""Compare exposure choices to isolated execution of the original native routine."""

import json
import unittest
from pathlib import Path

from .exposure_selection import ExposureSelection


class ExposureSelectionTests(unittest.TestCase):
  """Exercise saturation masks, disabled exposures, ties and invalid inputs."""

  def test_original_native_vectors(self) -> None:
    """All selections and values match the original HS7500 instructions."""
    fixture = json.loads(
      (Path(__file__).parent / "test_data/exposure_selection_vectors.json").read_text()
    )
    for vector in fixture["vectors"]:
      with self.subTest(vector=vector):
        flags = vector["saturation_flags"]
        result = ExposureSelection(
          tuple(value / 1000 for value in vector["exposure_milliseconds"]),
          tuple(vector["values"]),
          (bool(flags & 1), bool(flags & 2), bool(flags & 4)),
        )
        self.assertEqual(result.selected_index, vector["selected_index"])
        self.assertEqual(result.value, vector["result"])

  def test_invalid_inputs(self) -> None:
    """Reject mutable inputs, nonfinite times, noninteger counts and ambiguous flags."""
    for exposures, values, saturated in (
      ([0.54, 0.18, 0.06], (1, 2, 3), (False, False, False)),
      ((float("nan"), 0.18, 0.06), (1, 2, 3), (False, False, False)),
      ((-0.54, 0.18, 0.06), (1, 2, 3), (False, False, False)),
      ((True, 0.18, 0.06), (1, 2, 3), (False, False, False)),
      ((0.54, 0.18, 0.06), (1, -2, 3), (False, False, False)),
      ((0.54, 0.18, 0.06), (1, 2**32, 3), (False, False, False)),
      ((0.54, 0.18, 0.06), (1, 2.0, 3), (False, False, False)),
      ((0.54, 0.18, 0.06), (1, 2, 3), (False, 1, False)),
    ):
      with self.subTest(exposures=exposures, values=values, saturated=saturated):
        with self.assertRaises(ValueError):
          ExposureSelection(exposures, values, saturated)  # type: ignore[arg-type]
