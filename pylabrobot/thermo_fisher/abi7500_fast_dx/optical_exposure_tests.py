"""Replay optical-uniformity exposure search independently of pure-dye reference captures."""

import json
import unittest
from pathlib import Path

from .calibration_exposure import CalibrationExposureProbe, CalibrationExposureSearch

_REFERENCE = json.loads(
  (Path(__file__).parent / "test_data/optical_exposure_vectors.json").read_text()
)


class OpticalExposureTests(unittest.TestCase):
  """Recorded optical-calibration probes exercise the shared automatic exposure selector."""

  def test_all_optical_probe_decisions_and_final_exposures(self) -> None:
    """44 original-image probe statistics reproduce all five independently observed settings."""
    selected = []
    count = 0
    for item in _REFERENCE["filters"]:
      search = CalibrationExposureSearch()
      for probe in item["probes"]:
        self.assertEqual(search.next_milliseconds, probe["milliseconds"])
        search = search.add(
          CalibrationExposureProbe(probe["milliseconds"], tuple(probe["saturation_counts"]))
        )
        count += 1
      self.assertIsNone(search.next_milliseconds)
      self.assertEqual(search.selected_milliseconds, item["selected_milliseconds"])
      selected.append(search.selected_milliseconds)
    self.assertEqual(count, 44)
    self.assertEqual(selected, [1800, 945, 1350, 603, 450])
    self.assertEqual(
      [item["final_frame_count"] for item in _REFERENCE["filters"]], [1, 16, 16, 16, 16]
    )
